# ABOUTME: End-to-end tests for callback tools — tools with no worker-side body that an
# EXTERNAL client fulfills on its own machine, run against the Temporal time-skipping test
# server (the only faithful way — the in-workflow wait gate + the provide_callback_result
# update only exist when a real workflow executes). Demonstrates:
#   * a callback tool publishes tool_start -> callback_requested, parks until the client
#     provides a result, then callback_resolved(ok) -> tool_end, returning the validated value;
#   * a callback tool gets EXACTLY the same approval policy as any other tool — under
#     always_require_approvals it is gated FIRST (approval_requested -> resolved), and only then
#     does the callback_requested gate open;
#   * the client result is validated against the tool's declared output type: a bad payload is
#     rejected at the update boundary WITHOUT consuming the pending gate (resubmit works);
#   * a client-reported error surfaces to the model as a tool error (turn does not crash);
#   * an unfulfilled callback times out (callback_resolved(timeout) -> tool_error);
#   * the submission is idempotent (unknown / already-resolved ids are rejected);
#   * pending callbacks are discoverable via agent_status; close-while-pending fails the call.
#
# Run with: uv run pytest harness/test_callback_tools.py -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent import ToolApprovalPolicy
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
)
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
)


# ---------------------------------------------------------------------------
# Callback tools under test — bodies MUST be exactly `...`; the harness supplies the
# implementation and an attached client fulfills each call. One model-output tool, one
# scalar-output tool, and one with a short timeout for the no-result case.
# ---------------------------------------------------------------------------


class Echo(BaseModel):
    """A structured callback result."""

    value: str


@agent.callback_tool_defn()
async def echo_tool(text: str) -> Echo:
    """Echo the text back via the external client (structured output)."""
    ...


@agent.callback_tool_defn()
async def note_tool(text: str) -> str:
    """Return a note produced by the external client (scalar output)."""
    ...


@agent.callback_tool_defn(timeout=timedelta(seconds=5))
async def slow_tool(text: str) -> Echo:
    """A callback the client never fulfills — used to exercise the wait timeout."""
    ...


# text -> (fixed call id, tool, argument)
_SCENARIOS = {
    "echo": ("cb-echo", echo_tool, "hi"),
    "note": ("cb-note", note_tool, "buy milk"),
    "timeout": ("cb-timeout", slow_tool, "x"),
}


@workflow.defn
@agent.defn
class CallbackProbeAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Default: no gate, so the callback mechanics are isolated. The gating-parity test
            # overrides this per session via AgentConfig.approval_policy.
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        # Exposed via a query so a test can assert the outcome even after the workflow has
        # COMPLETED (relevant for the close-while-pending case, which ends the workflow).
        self._last_reply: str | None = None

    @workflow.query
    def last_reply(self) -> str | None:
        return self._last_reply

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Run the callback scenario selected by the message text."""
        call_id, tool, arg = _SCENARIOS[message.text]
        # Mirror a real agent loop: a failed callback (client error / timeout / close) surfaces
        # as a result rather than failing the turn.
        try:
            result = await self._runner.run_tool(call_id, tool, arg)
            self._last_reply = (
                f"ok:{result.value}" if isinstance(result, Echo) else f"ok:{result}"
            )
        except (agent.CallbackToolError, agent.ToolApprovalDenied) as e:
            self._last_reply = f"error:{e.reason}"
        return TextReply(text=self._last_reply)


# ---------------------------------------------------------------------------
# Fixture + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def env_and_client():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"callback-test-{uuid.uuid4()}"
    # Callback tools are inline (tool_defn) — no activities to register.
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CallbackProbeAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(
    client: Client, task_queue: str, *, config: AgentConfig | None = None
):
    return await client.start_workflow(
        CallbackProbeAgent.run,
        config if config is not None else AgentConfig(),
        id=f"CallbackProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(handle, text: str, expected_turn: int) -> None:
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="act", payload={"text": text}, expected_turn=expected_turn),
        result_type=AgentMessageReply,
    )


def _subscribe(client: Client, workflow_id: str):
    stream = WorkflowStreamClient.create(client, workflow_id)
    return stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )


async def _drain_to_turn_end(client: Client, workflow_id: str) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, workflow_id):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


def _types_for(events: list[AgentEvent], tool_id: str) -> list[str]:
    return [
        e.event.type for e in events if getattr(e.event, "tool_id", None) == tool_id
    ]


def _reply_text(events: list[AgentEvent]) -> str:
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    return reply.output["text"]


async def _await_callback_requested(
    client: Client, workflow_id: str, tool_id: str, collect: list[AgentEvent]
) -> AgentEvent:
    """Stream into ``collect`` until ``tool_id``'s callback_requested arrives; return it."""
    async with asyncio.timeout(30):
        async for item in _subscribe(client, workflow_id):
            collect.append(item.data)
            ev = item.data.event
            if (
                ev.type == AgentEventType.CALLBACK_REQUESTED
                and ev.tool_id == tool_id
            ):
                return item.data
    raise AssertionError("callback_requested never arrived")


# ---------------------------------------------------------------------------
# Core lifecycle
# ---------------------------------------------------------------------------


async def test_callback_tool_returns_client_result(env_and_client):
    """The client fulfills the call; the validated result becomes the tool's value."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "echo", expected_turn=1)
    agent_client = AgentClient(client, handle.id)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.CALLBACK_REQUESTED
                and ev.event.tool_id == "cb-echo"
            ):
                # The request carries the model-facing args + the expected output schema.
                assert ev.event.tool_input == {"text": "hi"}
                assert ev.event.output_schema.get("properties", {}).get("value")
                await agent_client.provide_callback_result(
                    "cb-echo", result={"value": "hi-back"}
                )
            if ev.event.type == AgentEventType.TURN_END:
                break

    # Full callback lifecycle, in order, nested inside the tool lifecycle.
    assert _types_for(events, "cb-echo") == [
        AgentEventType.TOOL_START,
        AgentEventType.CALLBACK_REQUESTED,
        AgentEventType.CALLBACK_RESOLVED,
        AgentEventType.TOOL_END,
    ]
    resolved = next(
        e.event for e in events if e.event.type == AgentEventType.CALLBACK_RESOLVED
    )
    assert resolved.outcome == "ok"
    assert _reply_text(events) == "ok:hi-back"


async def test_scalar_output_callback(env_and_client):
    """A callback tool with a scalar (str) output validates + returns a plain value."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "note", expected_turn=1)
    agent_client = AgentClient(client, handle.id)

    collected: list[AgentEvent] = []
    await _await_callback_requested(client, handle.id, "cb-note", collected)
    await agent_client.provide_callback_result("cb-note", result="noted: buy milk")
    events = await _drain_to_turn_end(client, handle.id)
    assert _reply_text(events) == "ok:noted: buy milk"


# ---------------------------------------------------------------------------
# Same approval policy as every other tool
# ---------------------------------------------------------------------------


async def test_callback_tool_is_gated_like_any_tool(env_and_client):
    """Under always_require_approvals a callback tool is APPROVAL-gated first; only after the
    human approves does the callback_requested gate open. Proves the callback tool goes through
    the identical policy path as every other tool (approval BEFORE the callback body)."""
    client, task_queue = env_and_client
    handle = await _start(
        client,
        task_queue,
        config=AgentConfig(approval_policy=ToolApprovalPolicy.always_require_approvals()),
    )
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "echo", expected_turn=1)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "cb-echo"
            ):
                await agent_client.approve_tool("cb-echo", approved=True)
            if (
                ev.event.type == AgentEventType.CALLBACK_REQUESTED
                and ev.event.tool_id == "cb-echo"
            ):
                await agent_client.provide_callback_result(
                    "cb-echo", result={"value": "hi-back"}
                )
            if ev.event.type == AgentEventType.TURN_END:
                break

    # Approval gate first, THEN the callback gate — the callback body only runs post-approval.
    assert _types_for(events, "cb-echo") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.CALLBACK_REQUESTED,
        AgentEventType.CALLBACK_RESOLVED,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "ok:hi-back"


async def test_denied_callback_never_requests_fulfillment(env_and_client):
    """A denied callback tool never reaches callback_requested — the approval gate stops it
    exactly as it would any other tool."""
    client, task_queue = env_and_client
    handle = await _start(
        client,
        task_queue,
        config=AgentConfig(approval_policy=ToolApprovalPolicy.always_require_approvals()),
    )
    agent_client = AgentClient(client, handle.id)
    await _send(handle, "echo", expected_turn=1)

    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == "cb-echo"
            ):
                await agent_client.approve_tool(
                    "cb-echo", approved=False, reason="nope"
                )
            if ev.event.type == AgentEventType.TURN_END:
                break

    assert _types_for(events, "cb-echo") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
    ]
    assert AgentEventType.CALLBACK_REQUESTED not in _types_for(events, "cb-echo")
    assert _reply_text(events) == "error:nope"


# ---------------------------------------------------------------------------
# Result validation, client errors, timeout
# ---------------------------------------------------------------------------


async def test_result_validated_against_output_type(env_and_client):
    """A result that fails the declared output type is rejected at the update boundary WITHOUT
    consuming the gate; a corrected resubmission then resolves it."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "echo", expected_turn=1)
    agent_client = AgentClient(client, handle.id)

    collected: list[AgentEvent] = []
    await _await_callback_requested(client, handle.id, "cb-echo", collected)

    # Wrong shape for Echo (missing 'value') -> rejected, gate stays pending.
    with pytest.raises(CallbackResultError) as bad:
        await agent_client.provide_callback_result("cb-echo", result={"nope": 1})
    assert bad.value.error_type == "MalformedCallbackResult"

    # The gate is still pending, so a correct result resolves it.
    await agent_client.provide_callback_result("cb-echo", result={"value": "fixed"})
    events = await _drain_to_turn_end(client, handle.id)
    assert _reply_text(events) == "ok:fixed"


async def test_client_reported_error_surfaces_to_model(env_and_client):
    """A client that cannot fulfill the call reports an error; it becomes the tool's error
    result (tool_error), and the turn continues rather than crashing."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "echo", expected_turn=1)
    agent_client = AgentClient(client, handle.id)

    collected: list[AgentEvent] = []
    await _await_callback_requested(client, handle.id, "cb-echo", collected)
    await agent_client.provide_callback_result(
        "cb-echo", error="permission denied on the user's machine"
    )
    events = collected + await _drain_to_turn_end(client, handle.id)

    types = _types_for(events, "cb-echo")
    assert AgentEventType.CALLBACK_RESOLVED in types
    assert types[-1] == AgentEventType.TOOL_ERROR
    resolved = next(
        e.event for e in events if e.event.type == AgentEventType.CALLBACK_RESOLVED
    )
    assert resolved.outcome == "error"
    assert _reply_text(events) == "error:permission denied on the user's machine"


async def test_unfulfilled_callback_times_out(env_and_client):
    """With a timeout and no result, the wait elapses: callback_resolved(timeout) -> tool_error
    (the time-skipping server advances to the timer)."""
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "timeout", expected_turn=1)

    events = await _drain_to_turn_end(client, handle.id)
    types = _types_for(events, "cb-timeout")
    assert types == [
        AgentEventType.TOOL_START,
        AgentEventType.CALLBACK_REQUESTED,
        AgentEventType.CALLBACK_RESOLVED,
        AgentEventType.TOOL_ERROR,
    ]
    resolved = next(
        e.event for e in events if e.event.type == AgentEventType.CALLBACK_RESOLVED
    )
    assert resolved.outcome == "timeout"
    assert _reply_text(events).startswith("error:callback timed out")


# ---------------------------------------------------------------------------
# Discovery, idempotency, close
# ---------------------------------------------------------------------------


async def test_pending_callback_visible_in_status(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "echo", expected_turn=1)
    agent_client = AgentClient(client, handle.id)

    await _await_callback_requested(client, handle.id, "cb-echo", [])

    pending = await agent_client.get_pending_callbacks()
    assert [p.tool_id for p in pending] == ["cb-echo"]
    assert pending[0].tool_name == "echo_tool"
    assert pending[0].tool_input == {"text": "hi"}
    assert pending[0].output_schema.get("properties", {}).get("value")

    await agent_client.provide_callback_result("cb-echo", result={"value": "done"})
    await _drain_to_turn_end(client, handle.id)
    assert await agent_client.get_pending_callbacks() == []


async def test_provide_result_is_idempotent(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "echo", expected_turn=1)
    agent_client = AgentClient(client, handle.id)

    await _await_callback_requested(client, handle.id, "cb-echo", [])

    with pytest.raises(CallbackResultError) as unknown:
        await agent_client.provide_callback_result(
            "does-not-exist", result={"value": "x"}
        )
    assert unknown.value.error_type == "UnknownCallback"

    await agent_client.provide_callback_result("cb-echo", result={"value": "x"})
    with pytest.raises(CallbackResultError) as dup:
        await agent_client.provide_callback_result("cb-echo", result={"value": "y"})
    assert dup.value.error_type == "CallbackAlreadyResolved"

    await _drain_to_turn_end(client, handle.id)


async def test_close_while_pending_fails_the_callback(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue)
    await _send(handle, "echo", expected_turn=1)

    pre_close: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            pre_close.append(item.data)
            if (
                item.data.event.type == AgentEventType.CALLBACK_REQUESTED
                and item.data.event.tool_id == "cb-echo"
            ):
                await handle.signal("close")
                break
    assert not any(e.event.type == AgentEventType.TOOL_END for e in pre_close)

    async with asyncio.timeout(30):
        await handle.result()
    completed = client.get_workflow_handle(handle.id)
    last_reply = await completed.query("last_reply", result_type=str)
    assert last_reply == "error:agent closed before the callback result arrived"
