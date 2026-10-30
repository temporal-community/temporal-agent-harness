# ABOUTME: End-to-end tests for the @agent.activity_tool_defn / @agent.tool_defn
# decorators + run_tool, run against the Temporal time-skipping test server (the only
# faithful way — the activity-side publishing path only exists when a real activity
# executes). Validates that:
#   * an ACTIVITY tool (@agent.activity_tool_defn) publishes tool_start/tool_end from
#     INSIDE its activity, carrying the call id run_tool parked for it;
#   * a WORKFLOW tool (@agent.tool_defn) publishes the same lifecycle in-process;
#   * each invocation's events carry the per-call tool_id (the same tool run twice
#     gets two distinct ids), proving the ambient _CURRENT_TOOL_ID is per-call.
#
# Run with: uv run pytest harness/test_agent_tool.py -v

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest_asyncio
from pydantic import BaseModel
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.exceptions import ApplicationError
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
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
    AgentMessageReply,
)
from temporal_agent_harness.harness.agent import Injected
from temporal_agent_harness.harness.agent_workflow import _injected_param_names


# ---------------------------------------------------------------------------
# Tools under test
# ---------------------------------------------------------------------------


@agent.activity_tool_defn()
async def echo_activity_tool(text: str) -> str:
    """An activity tool: runs in the activity worker, publishes from within."""
    return f"act:{text}"


@agent.tool_defn()
async def echo_workflow_tool(text: str) -> str:
    """A workflow tool: runs inline in the workflow, publishes in-process."""
    return f"wf:{text}"


class Receipt(BaseModel):
    order_id: str
    amount_cents: int
    tags: list[str]
    note: str | None = None


@agent.activity_tool_defn()
async def issue_receipt(order_id: str) -> Receipt:
    """An activity tool with a structured result."""
    return Receipt(order_id=order_id, amount_cents=1250, tags=["refund", "card"])


@agent.tool_defn()
async def record_nothing(text: str) -> None:
    """A workflow tool with no result."""


class _Opaque:
    pass


@agent.tool_defn()
async def return_opaque(text: str) -> object:
    """A workflow tool whose result has no JSON form."""
    return _Opaque()


@agent.tool_defn()
async def return_nan(text: str) -> dict[str, float]:
    """A workflow tool whose result holds a float JSON can't carry."""
    return {"ratio": float("nan")}


# ---------------------------------------------------------------------------
# Probe workflow: each text turn runs the activity tool twice + the workflow tool
# ---------------------------------------------------------------------------


@agent.defn
class ToolProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def probe(self, message: TextMessage) -> TextReply:
        """Run the activity tool twice + the workflow tool, then reply."""
        text = message.text

        # Activity tool: run_tool parks the per-call id, the decorator's
        # dispatcher executes the durable activity and ferries the
        # AgentToolContext in. Two calls → two distinct ids.
        await self._runner.run_tool("act-1", echo_activity_tool, text)
        await self._runner.run_tool("act-2", echo_activity_tool, text)

        # Workflow tool: invoked directly; publishes inline.
        await self._runner.run_tool("wf-1", echo_workflow_tool, text)

        return TextReply(text=f"done:{text}")

    @agent.accepts
    async def probe_outputs(self, message: TextMessage) -> TextReply:
        """Run tools whose results are a model, nothing, and two values with no JSON form."""
        await self._runner.run_tool("receipt", issue_receipt, message.text)
        await self._runner.run_tool("nothing", record_nothing, message.text)
        failures: list[str] = []
        for tool_id, tool in (("opaque", return_opaque), ("nan", return_nan)):
            try:
                await self._runner.run_tool(tool_id, tool, message.text)
            except ApplicationError as e:
                failures.append(f"{tool_id}:{e.type}")
        return TextReply(text=" ".join(failures))


# ---------------------------------------------------------------------------
# Fixture + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"agent-tool-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ToolProbeAgent],
        activities=[agent.tool_activity(echo_activity_tool), agent.tool_activity(issue_receipt)],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _collect_until_turn_end(
    client: Client, workflow_id: str
) -> list[AgentEvent]:
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    ):
        envelope: AgentEvent = item.data
        events.append(envelope)
        if envelope.event.type == AgentEventType.TURN_END:
            break
    return events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_activity_and_workflow_tools_publish_lifecycle(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        ToolProbeAgent.run,
        AgentConfig(),
        id=f"ToolProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe", payload={"text": "ping"}),
        result_type=AgentMessageReply,
    )

    events = await _collect_until_turn_end(client, handle.id)

    starts = [e.event for e in events if e.event.type == AgentEventType.TOOL_START]
    ends = [e.event for e in events if e.event.type == AgentEventType.TOOL_END]

    # Two activity-tool calls + one workflow-tool call, each a start + an end.
    by_id_start = {s.tool_id: s for s in starts}
    by_id_end = {e.tool_id: e for e in ends}
    assert set(by_id_start) == {"act-1", "act-2", "wf-1"}, by_id_start
    assert set(by_id_end) == {"act-1", "act-2", "wf-1"}, by_id_end

    # Activity tool: name + input captured inside the activity; output from the run.
    assert by_id_start["act-1"].tool_name == "echo_activity_tool"
    assert by_id_start["act-1"].tool_input == {"text": "ping"}
    assert by_id_end["act-1"].tool_output == "act:ping"

    # Workflow tool: published in-process.
    assert by_id_start["wf-1"].tool_name == "echo_workflow_tool"
    assert by_id_start["wf-1"].tool_input == {"text": "ping"}
    assert by_id_end["wf-1"].tool_output == "wf:ping"

    # Every tool event belongs to turn 1, and start precedes end per tool.
    assert all(e.turn_number == 1 for e in events if e.event.type.startswith("tool_"))
    for tool_id in ("act-1", "act-2", "wf-1"):
        order = [
            e.event.type
            for e in events
            if getattr(e.event, "tool_id", None) == tool_id
        ]
        assert order == [AgentEventType.TOOL_START, AgentEventType.TOOL_END], (
            tool_id,
            order,
        )


async def test_tool_end_carries_the_result_as_json(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        ToolProbeAgent.run,
        AgentConfig(),
        id=f"ToolProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe_outputs", payload={"text": "A-17"}),
        result_type=AgentMessageReply,
    )

    events = await _collect_until_turn_end(client, handle.id)
    ends = {e.event.tool_id: e.event for e in events if e.event.type == AgentEventType.TOOL_END}
    errors = {
        e.event.tool_id: e.event for e in events if e.event.type == AgentEventType.TOOL_ERROR
    }

    # A model is published as the JSON object it travels as, every field included.
    assert ends["receipt"].tool_output == {
        "order_id": "A-17",
        "amount_cents": 1250,
        "tags": ["refund", "card"],
        "note": None,
    }
    # No result is null, not "None".
    assert ends["nothing"].tool_output is None
    # A result with no JSON form fails the call instead of publishing a repr, and so does a
    # float JSON would turn into null.
    assert "opaque" not in ends and "nan" not in ends
    assert "_Opaque" in errors["opaque"].message
    assert errors["nan"].message.endswith(
        "tool 'return_nan' returned a value with no JSON form at $.ratio: nan"
    )
    [handler_end] = [
        e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END
    ]
    assert handler_end.output == {"text": "opaque:ToolOutputNotJson nan:ToolOutputNotJson"}


# ---------------------------------------------------------------------------
# Injected[...] parameter detection (pure unit tests)
# ---------------------------------------------------------------------------


def test_injected_param_names_detects_injected_annotations():
    """Parameters annotated Injected[...] are reported (these get hidden from the model
    and supplied by the workflow); ordinary parameters are not. Note this module uses
    `from __future__ import annotations`, so it also covers the stringized-annotation
    path that _injected_param_names must resolve."""

    async def fn(store: Injected[str], page_url: str, n: Injected[int] = 0) -> None: ...

    assert _injected_param_names(fn) == ("store", "n")


def test_injected_param_names_empty_when_none_injected():
    async def fn(page_url: str, limit: int = 5) -> None: ...

    assert _injected_param_names(fn) == ()
