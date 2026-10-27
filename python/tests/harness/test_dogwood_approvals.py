# ABOUTME: Tests for dogwood_evaluator, the auto mode evaluator that asks a VerifyToolCalls Nexus
# operation after recording the run's input events. Pure tests cover the verdict mapping and the
# input events. End-to-end tests run a real agent on the dev server
# against a stand-in handler that answers the way the Dogwood verifier does: json/protobuf payloads
# carrying the response messageTypes.

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import timedelta
from typing import Any, Literal

import nexusrpc
import nexusrpc.handler
import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_client import AgentClient
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
    ReceivedInput,
    ReceivedInputKind,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.dogwood_approvals import (
    Decision,
    PolicyCall,
    PolicyCalls,
    RecordInputEventsResponse,
    VerifyToolCallsResponse,
    decide,
    dogwood_evaluator,
    input_events,
)

# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


def _response(decision: str, reason: str = "") -> VerifyToolCallsResponse:
    return VerifyToolCallsResponse(decision=Decision.Value(decision), reason=reason)


@pytest.mark.parametrize(
    ("decision", "verdict"),
    [
        ("DECISION_APPROVED", AutoApprovalVerdict.APPROVE),
        ("DECISION_DENIED", AutoApprovalVerdict.DENY),
        ("DECISION_ESCALATE", AutoApprovalVerdict.ESCALATE),
    ],
)
def test_decide(decision: str, verdict: AutoApprovalVerdict) -> None:
    result = decide(_response(decision), details={"action": "p/refund"})
    assert result.verdict is verdict
    assert result.reason
    assert result.details["action"] == "p/refund"
    assert result.details["decision"] == decision


@pytest.mark.parametrize("decision", ["DECISION_RETRY_LATER", "DECISION_UNSPECIFIED"])
def test_no_decision_raises(decision: str) -> None:
    with pytest.raises(RuntimeError, match=decision):
        decide(_response(decision, "rate limited"))


def _verifier_payload(message: str, data: bytes) -> Payload:
    """A payload as the Rust verifiers build it (verifiers/host/src/lib.rs, json_proto_payload)."""
    return Payload(
        metadata={
            "encoding": b"json/protobuf",
            "messageType": f"temporal_untrusted_workers.toolpolicy.v1.{message}".encode(),
        },
        data=data,
    )


def test_decodes_the_verifiers_response_payloads() -> None:
    [response, recorded] = pydantic_data_converter.payload_converter.from_payloads(
        [
            _verifier_payload(
                "VerifyToolCallsResponse",
                b'{"decision":"DECISION_ESCALATE","reason":"tool p/x escalated by Dogwood policy x"}',
            ),
            _verifier_payload("RecordInputEventsResponse", b"{}"),
        ]
    )
    assert isinstance(response, VerifyToolCallsResponse)
    assert response.decision == Decision.Value("DECISION_ESCALATE")
    assert response.reason == "tool p/x escalated by Dogwood policy x"
    assert isinstance(recorded, RecordInputEventsResponse)


def test_input_events() -> None:
    received = [
        ReceivedInput(
            kind=ReceivedInputKind.MESSAGE, name="ask", payload={"text": "hi"}, id="upd-1"
        ),
        ReceivedInput(
            kind=ReceivedInputKind.TOOL_RESULT,
            name="refund",
            payload="ok",
            id="t1",
            arguments={"amount": 20},
        ),
    ]
    events = [
        e.model_dump(mode="json", by_alias=True) for e in input_events(received, action_prefix="p")
    ]
    value = "type.googleapis.com/google.protobuf.Value"
    assert events == [
        {
            "kind": "INPUT_EVENT_KIND_UPDATE",
            "name": "ask",
            "payloads": [{"@type": value, "value": {"text": "hi"}}],
            "updateId": "upd-1",
        },
        {
            "kind": "INPUT_EVENT_KIND_ACTIVITY_RESULT",
            "name": "p/refund/result",
            "payloads": [
                {"@type": value, "value": "ok"},
                {"@type": value, "value": {"amount": 20}},
            ],
            "updateId": "tool-result/t1",
        },
    ]


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

ENDPOINT = "dogwood-test-tool-policy"
POLICY_TASK_QUEUE = f"tool-policy-{uuid.uuid4()}"
PREFIX = "probe"

# The stand-in policy: maps each request to a response, and records what it was asked and
# what it was told the run received.
_POLICY: list[Callable[[dict[str, Any]], VerifyToolCallsResponse]] = []
_ASKED: list[dict[str, Any]] = []
_RECORDED: list[dict[str, Any]] = []


@nexusrpc.handler.service_handler(name="tool-policy")
class FakeToolPolicy:
    @nexusrpc.handler.sync_operation(name="VerifyToolCalls")
    async def verify(
        self, ctx: nexusrpc.handler.StartOperationContext, request: dict[str, Any]
    ) -> VerifyToolCallsResponse:
        _ASKED.append(request)
        return _POLICY[0](request)

    @nexusrpc.handler.sync_operation(name="RecordInputEvents")
    async def record(
        self, ctx: nexusrpc.handler.StartOperationContext, request: dict[str, Any]
    ) -> RecordInputEventsResponse:
        _RECORDED.append(request)
        return RecordInputEventsResponse()


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


@agent.activity_tool_defn()
async def run_sql(sql: str) -> str:
    """Run a query."""
    return f"rows:{sql}"


class Lease(BaseModel):
    term: Literal["15 minutes", "1 hour"]


@agent.callback_tool_defn()
async def grant_lease(table: str) -> Lease: ...


async def sql_policy_calls(ctx: agent.AutoApprovalContext) -> PolicyCalls:
    """A stand-in for a builder that parses the query: the "tables" are its words."""
    sql = ctx.tool_input["sql"]
    if sql == "unparseable":
        raise ValueError("cannot parse the query")
    if sql == "broken":
        return PolicyCalls(
            arg0={"statement": "SELECT", "error": "no column x"},
            explanation="DuckDB: no column x",
        )
    return PolicyCalls(
        arg0={"statement": "SELECT"},
        checks=[PolicyCall(tool="read_table", arg0={"table": table}) for table in sql.split()],
    )


@agent.defn
class DogwoodProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.auto_mode(
                pre_approved_tools=["run_orders", "grant_lease"]
            ),
            auto_approval_criteria_default=AutoApprovalCriteria(
                sets={"dogwood": AutoApprovalCriteriaSet(effect="Judged by the Dogwood policy.")},
                default="dogwood",
            ),
            auto_mode_evaluator=dogwood_evaluator(
                endpoint=ENDPOINT,
                action_prefix=PREFIX,
                policy_calls={"run_sql": sql_policy_calls},
            ),
        )
        self._code_tool = agent.code_mode_tool(
            [lookup_order, cancel_order, grant_lease], name="run_orders"
        )

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Try one refund of the amount in the message."""
        try:
            result = await self._runner.run_tool("t1", refund, int(message.text))
        except agent.ToolApprovalDenied as e:
            result = f"denied:{e.reason}"
        return TextReply(text=result)

    @agent.accepts
    async def query(self, message: TextMessage) -> TextReply:
        """Run the query in the message."""
        try:
            result = await self._runner.run_tool("t1", run_sql, sql=message.text)
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
    env: WorkflowEnvironment,
    message_type: str,
    text: str,
    *,
    until: str = AgentEventType.TURN_END,
    answer: dict[str, Any] | None = None,
) -> list[Any]:
    """Send one message and collect turn events until ``until``, answering any callback the
    turn asks for with ``answer``."""
    _ASKED.clear()
    _RECORDED.clear()
    task_queue = f"dogwood-probe-{uuid.uuid4()}"
    # Workers start here rather than in the fixture, which runs on another event loop.
    async with Worker(
        env.client, task_queue=POLICY_TASK_QUEUE, nexus_service_handlers=[FakeToolPolicy()]
    ), Worker(
        env.client,
        task_queue=task_queue,
        workflows=[DogwoodProbeAgent],
        activities=[agent.tool_activity(refund), agent.tool_activity(run_sql)],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            DogwoodProbeAgent.run,
            AgentConfig(),
            id=f"dogwood-probe-{uuid.uuid4()}",
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
                if answer is not None and item.data.event.type == AgentEventType.CALLBACK_REQUESTED:
                    await AgentClient(env.client, handle.id).provide_callback_result(
                        item.data.event.tool_id, result=answer
                    )
                if item.data.event.type == until:
                    break
        await handle.terminate()
    return events


def _of(events: list[Any], event_type: str) -> list[Any]:
    return [e for e in events if e.type == event_type]


def _reply(events: list[Any]) -> str:
    return _of(events, AgentEventType.MESSAGE_HANDLER_END)[0].output["text"]


async def test_allowed_call_runs_unattended(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(env, "act", "20")

    assert _reply(events) == "refunded:20"
    [request] = _ASKED
    assert request["caller"]["subject"] == "DogwoodProbeAgent"
    assert request["caller"]["workflowId"].startswith("dogwood-probe-")
    assert request["caller"]["runId"]
    assert request["toolCalls"] == [
        {
            "name": "probe/refund",
            "parameters": [
                {"@type": "type.googleapis.com/google.protobuf.Value", "value": {"amount": 20}}
            ],
        }
    ]
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.evaluator == "dogwood_evaluator"
    assert ended.verdict == AutoApprovalVerdict.APPROVE
    assert ended.details["action"] == "probe/refund"

    # Before asking, the verifier was told about the message that started the turn.
    [recorded] = _RECORDED
    assert recorded["caller"] == request["caller"]
    assert recorded["fromHistoryStart"] is True
    [message] = recorded["events"]
    assert message["kind"] == "INPUT_EVENT_KIND_UPDATE"
    assert message["name"] == "act"
    assert message["payloads"][0]["value"] == {"text": "20"}
    assert message["updateId"]


async def test_forbidden_call_is_denied(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_DENIED", "tool probe/refund denied by Dogwood policy refund-limit"))
    events = await _run(env, "act", "5000")
    assert _reply(events) == "denied:tool probe/refund denied by Dogwood policy refund-limit"
    assert not _of(events, AgentEventType.TOOL_START)


async def test_escalated_call_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(
        _response("DECISION_ESCALATE", "tool probe/refund escalated by Dogwood policy refund-review")
    )
    events = await _run(
        env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED
    )
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.verdict == AutoApprovalVerdict.ESCALATE
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


async def test_retry_later_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_RETRY_LATER", "rate limited"))
    events = await _run(env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert "DECISION_RETRY_LATER" in error.message
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


async def test_policy_failure_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _fail()
    events = await _run(env, "act", "20", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert error.evaluator == "dogwood_evaluator"
    assert not _of(events, AgentEventType.TOOL_APPROVAL_RESOLVED)


SCRIPT = """\
import asyncio
async def main():
    for order_id in ["A-1", "A-2"]:
        await cancel_order(order_id)
    return await lookup_order("A-1")
asyncio.run(main())
"""


async def test_host_calls_are_judged_by_their_arguments(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(env, "script", SCRIPT)

    assert "order:A-1" in _reply(events)
    # The script itself is pre-approved; each host call it makes is judged on its own.
    names = [request["toolCalls"][0]["name"] for request in _ASKED]
    assert names == ["probe/cancel_order", "probe/cancel_order", "probe/lookup_order"]
    assert _ASKED[0]["toolCalls"][0]["parameters"][0]["value"] == {"order_id": "A-1"}

    # Each evaluation first hands over only what arrived since the last one: the message before
    # the first host call is judged, then each host call's result before the next one is.
    # A result carries the call's arguments as its second payload.
    batches = [
        [(e["kind"], e["name"], [p["value"] for p in e["payloads"]]) for e in r["events"]]
        for r in _RECORDED
    ]
    result = "INPUT_EVENT_KIND_ACTIVITY_RESULT"
    assert batches == [
        [("INPUT_EVENT_KIND_UPDATE", "script", [{"text": SCRIPT}])],
        [(result, "probe/cancel_order/result", ["cancelled:A-1", {"order_id": "A-1"}])],
        [(result, "probe/cancel_order/result", ["cancelled:A-2", {"order_id": "A-2"}])],
    ]
    assert [r["fromHistoryStart"] for r in _RECORDED] == [True, False, False]


LEASE_SCRIPT = """\
import asyncio
async def main():
    lease = await grant_lease(table="risk.loans")
    return await lookup_order(lease["term"])
asyncio.run(main())
"""


async def test_a_persons_answer_reaches_the_policy_with_what_they_were_asked(
    env: WorkflowEnvironment,
) -> None:
    """A script waits for a person through a callback tool; their answer is recorded beside
    the arguments they were shown, before the next host call is judged."""
    _answer(_response("DECISION_APPROVED"))
    events = await _run(env, "script", LEASE_SCRIPT, answer={"term": "1 hour"})

    assert "order:1 hour" in _reply(events)
    # The callback tool is pre-approved; only lookup_order reaches the policy.
    assert [request["toolCalls"][0]["name"] for request in _ASKED] == ["probe/lookup_order"]
    # Recorded before lookup_order was judged: the message, then the person's answer.
    [batch] = _RECORDED
    [message, lease] = batch["events"]
    assert message["name"] == "script"
    assert lease["name"] == "probe/grant_lease/result"
    assert [p["value"] for p in lease["payloads"]] == [{"term": "1 hour"}, {"table": "risk.loans"}]


async def test_policy_calls_send_the_builders_checks_in_one_request(
    env: WorkflowEnvironment,
) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(env, "query", "branches loans")

    assert _reply(events) == "rows:branches loans"
    [request] = _ASKED
    value = "type.googleapis.com/google.protobuf.Value"
    assert request["toolCalls"] == [
        {"name": "probe/run_sql", "parameters": [{"@type": value, "value": {"statement": "SELECT"}}]},
        {"name": "probe/read_table", "parameters": [{"@type": value, "value": {"table": "branches"}}]},
        {"name": "probe/read_table", "parameters": [{"@type": value, "value": {"table": "loans"}}]},
    ]
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.details["parameter"] == {"statement": "SELECT"}
    assert ended.details["checks"] == [
        {"action": "probe/read_table", "parameter": {"table": "branches"}},
        {"action": "probe/read_table", "parameter": {"table": "loans"}},
    ]


async def test_a_denial_carries_the_builders_explanation(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_DENIED", "tool probe/run_sql denied by Dogwood policy plan"))
    events = await _run(env, "query", "broken")

    assert _reply(events) == (
        "denied:tool probe/run_sql denied by Dogwood policy plan\n\nDuckDB: no column x"
    )
    # The policy saw the facts, not the explanation.
    [request] = _ASKED
    assert request["toolCalls"][0]["parameters"][0]["value"] == {
        "statement": "SELECT",
        "error": "no column x",
    }
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert ended.details["explanation"] == "DuckDB: no column x"


async def test_an_approval_ignores_the_explanation(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(env, "query", "broken")

    assert _reply(events) == "rows:broken"
    [ended] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED)
    assert "DuckDB" not in (ended.reason or "")


async def test_failing_policy_call_builder_goes_to_a_human(env: WorkflowEnvironment) -> None:
    _answer(_response("DECISION_APPROVED"))
    events = await _run(
        env, "query", "unparseable", until=AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR
    )
    [error] = _of(events, AgentEventType.AUTO_APPROVAL_EVALUATION_ERROR)
    assert "cannot parse the query" in error.message
    assert _ASKED == []
