# ABOUTME: Tests for cedar_evaluator, the auto mode evaluator that asks a VerifyToolCalls Nexus
# operation. Pure tests cover the verdict mapping and the script parameter. End-to-end tests run
# a real agent on the dev server against a stand-in handler that answers the way the Cedar
# verifier does: a json/protobuf payload carrying VerifyToolCallsResponse's messageType.

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import timedelta
from typing import Any

import nexusrpc
import nexusrpc.handler
import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AutoApprovalCriteria,
    AutoApprovalCriteriaSet,
    AutoApprovalVerdict,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.cedar_approvals import (
    VerifyToolCallsResponse,
    cedar_evaluator,
    decide,
    script_parameter,
)
from temporal_agent_harness.harness.code_mode import analyze_script
from temporal_agent_harness.harness.jev_approvals import (
    JEV_CLASSIFY_ACTIVITY,
    JevApprovalRequest,
    JevClassificationAnswer,
    ScriptFeature,
    jev_script_classifier,
)

# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "verdict"),
    [
        (VerifyToolCallsResponse(allowed=True), AutoApprovalVerdict.APPROVE),
        (
            VerifyToolCallsResponse(
                allowed=False, reason="tool p/refund denied by Cedar policy refund-limit"
            ),
            AutoApprovalVerdict.DENY,
        ),
        (
            VerifyToolCallsResponse(
                allowed=False, reason="tool p/refund not permitted by any Cedar policy"
            ),
            AutoApprovalVerdict.ESCALATE,
        ),
        (VerifyToolCallsResponse(), AutoApprovalVerdict.ESCALATE),
    ],
)
def test_decide(response: VerifyToolCallsResponse, verdict: AutoApprovalVerdict) -> None:
    decision = decide(response, details={"action": "p/refund"})
    assert decision.verdict is verdict
    assert decision.reason
    assert decision.details["action"] == "p/refund"
    assert decision.details["allowed"] is response.allowed


def test_decodes_the_cedar_verifiers_response_payload() -> None:
    """The payload the Rust verifier builds (verifiers/cedar/src/main.rs, response_payload)."""
    payload = Payload(
        metadata={
            "encoding": b"json/protobuf",
            "messageType": b"temporal_untrusted_workers.toolpolicy.v1.VerifyToolCallsResponse",
        },
        data=b'{"allowed":false,"reason":"tool p/x denied by Cedar policy no-x"}',
    )
    [response] = pydantic_data_converter.payload_converter.from_payloads([payload])
    assert isinstance(response, VerifyToolCallsResponse)
    assert (response.allowed, response.reason) == (False, "tool p/x denied by Cedar policy no-x")


def test_script_parameter() -> None:
    facts = analyze_script(
        "for x in xs:\n    book(x)\nsearch()\n", {"book", "search", "unused"}
    )
    assert script_parameter(facts, {"unbounded_iteration": 7}) == {
        "calls": ["book", "search"],
        "uses": {
            "book": {"call_sites": 1, "in_loop": True, "concurrent": False, "indirect": False},
            "search": {"call_sites": 1, "in_loop": False, "concurrent": False, "indirect": False},
        },
        "has_while_loop": False,
        "dynamic_code": False,
        "classification": {"unbounded_iteration": 7},
    }
    assert "classification" not in script_parameter(facts)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

ENDPOINT = "cedar-test-tool-policy"
POLICY_TASK_QUEUE = f"tool-policy-{uuid.uuid4()}"
PREFIX = "probe"

# The stand-in policy: maps each request to a response, and records what it was asked.
_POLICY: list[Callable[[dict[str, Any]], VerifyToolCallsResponse]] = []
_ASKED: list[dict[str, Any]] = []


@nexusrpc.handler.service_handler(name="tool-policy")
class FakeToolPolicy:
    @nexusrpc.handler.sync_operation(name="VerifyToolCalls")
    async def verify(
        self, ctx: nexusrpc.handler.StartOperationContext, request: dict[str, Any]
    ) -> VerifyToolCallsResponse:
        _ASKED.append(request)
        return _POLICY[0](request)


def _answer(response: VerifyToolCallsResponse) -> None:
    _POLICY[:] = [lambda request: response]


def _fail() -> None:
    def fail(request: dict[str, Any]) -> VerifyToolCallsResponse:
        raise nexusrpc.HandlerError("policy store unreachable", type=nexusrpc.HandlerErrorType.BAD_REQUEST)

    _POLICY[:] = [fail]


@agent.activity_tool_defn()
async def refund(amount: int) -> str:
    """Refund a customer."""
    return f"refunded:{amount}"


@agent.tool_defn()
async def lookup_order(order_id: str) -> str:
    """Look up an order."""
    return f"order:{order_id}"


@agent.tool_defn()
async def cancel_order(order_id: str) -> str:
    """Cancel an order."""
    return f"cancelled:{order_id}"


_FEATURES = (
    ScriptFeature(key="unbounded_iteration", question="Unbounded?", yes="Yes.", no="No."),
)


@activity.defn(name=JEV_CLASSIFY_ACTIVITY)
async def fake_jev_classify(request: JevApprovalRequest) -> JevClassificationAnswer:
    return JevClassificationAnswer(
        model="jev-test", request_id="req", answers={"unbounded_iteration": 0.034}
    )


@agent.defn
class CedarProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(),
            auto_approval_criteria_default=AutoApprovalCriteria(
                sets={"cedar": AutoApprovalCriteriaSet(effect="Judged by the Cedar policy.")},
                default="cedar",
            ),
            auto_mode_evaluator=cedar_evaluator(
                endpoint=ENDPOINT,
                action_prefix=PREFIX,
                script_classifier=jev_script_classifier(_FEATURES),
            ),
        )
        self._code_tool = agent.code_mode_tool([lookup_order, cancel_order], name="run_orders")

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund of the amount in the message."""
        try:
            result = await self._runner.run_tool("t1", refund, int(message.text))
        except agent.ToolApprovalDenied as e:
            result = f"denied:{e.reason}"
        return TextReply(text=result)

    @agent.accepts
    async def script(self, message: TextMessage) -> TextReply:
        """Run the script in the message."""
        try:
            result = await self._runner.run_tool("t1", self._code_tool, script=message.text)
        except agent.ToolApprovalDenied as e:
            result = f"denied:{e.reason}"
        return TextReply(text=result)


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_extra_args=[
            "--dynamic-config-value",
            'system.system.refreshNexusEndpointsMinWait="0s"',
        ],
    )
    await env.create_nexus_endpoint(ENDPOINT, POLICY_TASK_QUEUE)
    yield env
    await env.shutdown()


async def _run(
    env: WorkflowEnvironment, message_type: str, text: str, *, until: str = AgentEventType.TURN_END
) -> list[Any]:
    """Send one message and collect turn events until ``until``."""
    _ASKED.clear()
    task_queue = f"cedar-probe-{uuid.uuid4()}"
    # Workers start here rather than in the fixture, which runs on another event loop.
    async with Worker(
        env.client, task_queue=POLICY_TASK_QUEUE, nexus_service_handlers=[FakeToolPolicy()]
    ), Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CedarProbeAgent],
        activities=[agent.tool_activity(refund), fake_jev_classify],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            CedarProbeAgent.run,
            AgentConfig(),
            id=f"cedar-probe-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle.execute_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type=message_type, payload={"text": text}),
            result_type=AgentMessageReply,
        )
        events: list[Any] = []
        stream = WorkflowStreamClient.create(env.client, handle.id)
        async with asyncio.timeout(30):
            async for item in stream.subscribe(
                topics=[TURN_EVENTS_TOPIC],
                from_offset=0,
                result_type=AgentEvent,
                poll_cooldown=timedelta(milliseconds=10),
            ):
                events.append(item.data.event)
                if item.data.event.type == until:
                    break
        await handle.terminate()
    return events


def _of(events: list[Any], event_type: str) -> list[Any]:
    return [e for e in events if e.type == event_type]


def _reply(events: list[Any]) -> str:
    return _of(events, AgentEventType.MESSAGE_HANDLER_END)[0].output["text"]


async def test_allowed_call_runs_unattended(env: WorkflowEnvironment) -> None:
    _answer(VerifyToolCallsResponse(allowed=True))
    events = await _run(env, "act", "20")

    assert _reply(events) == "refunded:20"
    [request] = _ASKED
    assert request["caller"]["subject"] == "CedarProbeAgent"
    assert request["caller"]["workflowId"].startswith("cedar-probe-")
    assert request["toolCalls"] == [
        {
            "name": "probe/refund",
            "parameters": [
                {"@type": "type.googleapis.com/google.protobuf.Value", "value": {"amount": 20}}
            ],
        }
    ]
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.evaluator == "cedar_evaluator"
    assert ended.verdict == AutoApprovalVerdict.APPROVE
    assert ended.details["action"] == "probe/refund"


async def test_forbidden_call_is_denied(env: WorkflowEnvironment) -> None:
    _answer(
        VerifyToolCallsResponse(
            allowed=False, reason="tool probe/refund denied by Cedar policy refund-limit"
        )
    )
    events = await _run(env, "act", "5000")
    assert _reply(events) == "denied:tool probe/refund denied by Cedar policy refund-limit"
    assert not _of(events, AgentEventType.TOOL_START)


async def test_call_no_policy_permits_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(
        VerifyToolCallsResponse(
            allowed=False, reason="tool probe/refund not permitted by any Cedar policy"
        )
    )
    events = await _run(
        env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED
    )
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict == AutoApprovalVerdict.ESCALATE
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


async def test_policy_failure_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _fail()
    events = await _run(env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert error.evaluator == "cedar_evaluator"
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


SCRIPT = """\
import asyncio
async def main():
    for order_id in ["A-1", "A-2"]:
        await cancel_order(order_id)
    return await lookup_order("A-1")
asyncio.run(main())
"""


async def test_script_is_judged_by_its_facts_and_host_calls_by_their_arguments(
    env: WorkflowEnvironment,
) -> None:
    _answer(VerifyToolCallsResponse(allowed=True))
    events = await _run(env, "script", SCRIPT)

    assert "order:A-1" in _reply(events)
    names = [request["toolCalls"][0]["name"] for request in _ASKED]
    assert names == [
        "probe/run_orders",
        "probe/cancel_order",
        "probe/cancel_order",
        "probe/lookup_order",
    ]
    script_value = _ASKED[0]["toolCalls"][0]["parameters"][0]["value"]
    assert script_value == {
        "calls": ["cancel_order", "lookup_order"],
        "uses": {
            "cancel_order": {"call_sites": 1, "in_loop": True, "concurrent": False, "indirect": False},
            "lookup_order": {"call_sites": 1, "in_loop": False, "concurrent": False, "indirect": False},
        },
        "has_while_loop": False,
        "dynamic_code": False,
        "classification": {"unbounded_iteration": 3},
    }
    assert _ASKED[1]["toolCalls"][0]["parameters"][0]["value"] == {"order_id": "A-1"}


async def test_unparseable_script_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(VerifyToolCallsResponse(allowed=True))
    events = await _run(
        env, "script", "def broken(:\n", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR
    )
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert "syntax" in error.message.lower()
    assert _ASKED == []
