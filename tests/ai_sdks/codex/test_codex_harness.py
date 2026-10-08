# ABOUTME: Tests for the Codex adapter (temporal_agent_harness.ai_sdks.codex_harness). The pure-Python
# tests cover tool adaptation. The end-to-end tests run the REAL `codex app-server` binary (the
# `codex` extra) against a fake Responses API, inside the workflow sandbox, on a local dev server:
#   * a host tool the model calls runs through the harness (tool_requested -> tool_start -> tool_end
#     under the model's call id) and its result reaches the model;
#   * built-in Codex tools are gone: the model is offered only the host tools;
#   * the turn streams reply_delta and a model-interaction span with token usage;
#   * a gated tool waits for a human approval before it runs;
#   * a segment that crashes after a tool already ran is retried on a fresh CODEX_HOME rebuilt from
#     the rollout the workflow holds: the tool does NOT run again and the model sees the real result.
#
# No credentials needed. Run with: uv run pytest tests/ai_sdks/codex -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from importlib.util import find_spec
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporalio.openai_codex import CODEX_RUN_SEGMENT_ACTIVITY
from temporalio.openai_codex._app_server import AppServer
from temporalio.openai_codex.testing import FakeResponsesServer

from temporal_agent_harness.ai_sdks.codex_harness import (
    CodexHarnessPlugin,
    as_codex_tool,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TOOL_APPROVAL_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    ToolApprovalDecision,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from ._codex_e2e_workflow import (
    CodexAgent,
    CodexParentAgent,
    EchoChildAgent,
    GatedCodexAgent,
    lookup,
    record,
)

requires_codex_binary = pytest.mark.skipif(
    find_spec("codex_cli_bin") is None,
    reason="needs the `codex` extra (openai-codex-cli-bin)",
)


# ---------------------------------------------------------------------------
# Tool adaptation (pure Python)
# ---------------------------------------------------------------------------


def test_as_codex_tool_derives_the_dynamic_tool_spec():
    tool = as_codex_tool(None, lookup)  # type: ignore[arg-type]  # the runner is only used on a call
    assert tool.spec.name == "lookup"
    assert tool.spec.description == "Look up a ticket by id."
    assert tool.spec.input_schema["type"] == "object"
    assert tool.spec.input_schema["properties"]["q"]["type"] == "string"
    assert tool.spec.input_schema["required"] == ["q"]


def test_as_codex_tool_rejects_a_plain_function():
    async def not_a_harness_tool(q: str) -> str:
        return q

    with pytest.raises(TypeError, match="not a harness tool"):
        as_codex_tool(None, not_a_harness_tool)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# End to end against the real codex binary
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    monkeypatch.setenv("CODEX_TEST_LEDGER", str(ledger))
    fake = FakeResponsesServer()
    await fake.start()
    plugins = [
        CodexHarnessPlugin(config_overrides=fake.config_overrides, home_root=str(tmp_path)),
        AgentHarnessPlugin(tools=[lookup, record]),
    ]
    env = await WorkflowEnvironment.start_local(plugins=plugins)
    task_queue = f"codex-{uuid.uuid4()}"
    # NOTE: the default sandboxed workflow runner, deliberately.
    async with Worker(env.client, task_queue=task_queue, workflows=[CodexAgent, GatedCodexAgent, CodexParentAgent, EchoChildAgent]):
        try:
            yield env.client, task_queue, fake, ledger
        finally:
            await env.shutdown()
            await fake.stop()


async def _start(client: Client, task_queue: str, workflow_cls):
    handle = await client.start_workflow(
        workflow_cls.run,
        AgentConfig(),
        id=f"codex-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    return handle


def _ask(handle, text: str):
    return handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": text}, expected_turn=1),
        result_type=AgentMessageReply,
    )


async def _events_until_turn_end(client: Client, handle) -> list[AgentEvent]:
    stream = WorkflowStreamClient.create(client, handle.id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(90):
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            if item.data.event.type == AgentEventType.TURN_END:
                break
    return events


def _tool_names_offered(fake: FakeResponsesServer) -> set[str]:
    names: set[str] = set()
    for body in fake.requests:
        for tool in body.get("tools", []):
            names.add(tool.get("name") or tool.get("type"))
    return names


async def _turn(client: Client, handle, text: str) -> tuple[list[AgentEvent], str]:
    """Send one message and collect the turn's events plus the handler's reply text."""
    await _ask(handle, text)
    events = await _events_until_turn_end(client, handle)
    return events, _reply_text(events)


def _reply_text(events: list[AgentEvent]) -> str:
    ends = [e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END]
    assert len(ends) == 1, [e.event.type for e in events]
    return ends[0].output["text"]


PROMPT = 'Please look up the ticket and record it.\nCALL:lookup|{"q":"42"}'


@requires_codex_binary
async def test_host_tool_runs_through_the_harness(stack):
    client, task_queue, fake, _ledger = stack
    handle = await _start(client, task_queue, CodexAgent)
    events, reply = await _turn(client, handle, PROMPT)

    # The model's tool result reached the model and came back in its answer.
    assert "ticket-42: resolved" in reply

    # One tool card, correlated under the model's own call id.
    call_events = [e.event for e in events if getattr(e.event, "tool_name", None) == "lookup"]
    assert [e.type for e in call_events] == [
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert len({e.tool_id for e in call_events}) == 1
    assert call_events[0].tool_input == {"q": "42"}
    assert call_events[-1].tool_output == "ticket-42: resolved"
    assert not [e for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_ERROR]

    # Codex's own built-in tools are off in EVERY request, including the segments that resume the
    # thread (the thread config does not persist across a resume): only the host tools are offered.
    assert len(fake.requests) >= 2
    offered = _tool_names_offered(fake)
    assert {"lookup", "record"} <= offered
    assert not offered & {"exec_command", "write_stdin", "apply_patch", "view_image", "create_goal"}


@requires_codex_binary
async def test_turn_streams_reply_and_token_usage(stack):
    client, task_queue, _fake, _ledger = stack
    handle = await _start(client, task_queue, CodexAgent)
    events = [e.event for e in (await _turn(client, handle, PROMPT))[0]]

    deltas = "".join(e.text for e in events if e.type == AgentEventType.REPLY_DELTA)
    assert "ticket-42: resolved" in deltas
    started = [e for e in events if e.type == AgentEventType.MODEL_INTERACTION_STARTED]
    ended = [e for e in events if e.type == AgentEventType.MODEL_INTERACTION_ENDED]
    # One segment before the tool call and one after it: two spans, each closed.
    assert len(started) == len(ended) == 2
    # The span that ends the turn reports usage. (The span cut short by the tool call may not:
    # that segment's app-server is stopped as soon as the call arrives.)
    assert ended[-1].usage is not None and ended[-1].usage.total_tokens


@requires_codex_binary
async def test_activity_tool_effect_runs_once(stack):
    client, task_queue, _fake, ledger = stack
    handle = await _start(client, task_queue, CodexAgent)
    _events, reply = await _turn(client, handle, 'Record it.\nCALL:record|{"note":"hello"}')
    assert "recorded:hello" in reply
    assert ledger.read_text().split() == ["hello"]


@requires_codex_binary
async def test_gated_tool_waits_for_approval(stack):
    client, task_queue, _fake, _ledger = stack
    handle = await _start(client, task_queue, GatedCodexAgent)
    await _ask(handle, PROMPT)

    stream = WorkflowStreamClient.create(client, handle.id)
    events: list[AgentEvent] = []
    approved = False
    async with asyncio.timeout(90):
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            events.append(item.data)
            event = item.data.event
            if event.type == AgentEventType.TOOL_APPROVAL_REQUESTED and not approved:
                assert event.tool_name == "lookup"
                # Parked on the gate: the tool has not started.
                assert not [e for e in events if e.event.type == AgentEventType.TOOL_START]
                await handle.execute_update(
                    TOOL_APPROVAL_UPDATE, ToolApprovalDecision(tool_id=event.tool_id, approved=True)
                )
                approved = True
            if event.type == AgentEventType.TURN_END:
                break
    assert approved
    assert "ticket-42: resolved" in _reply_text(events)
    types = [e.event.type for e in events if getattr(e.event, "tool_name", None) == "lookup"]
    assert types[:4] == [
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
    ]


@requires_codex_binary
async def test_crash_after_a_tool_ran_does_not_run_it_again(
    stack, monkeypatch: pytest.MonkeyPatch
):
    client, task_queue, fake, ledger = stack
    original_call = AppServer.call

    async def crashing_call(self, method, params=None):
        result = await original_call(self, method, params)
        # The segment that resumes after the tool: SIGKILL the app-server right after the turn
        # starts, on the first attempt only, as a dying Worker would.
        if (
            method == "turn/start"
            and params["input"][0]["text"] == "Continue."
            and activity.info().attempt == 1
        ):
            self.kill()
        return result

    monkeypatch.setattr(AppServer, "call", crashing_call)

    handle = await _start(client, task_queue, CodexAgent)
    _events, reply = await _turn(client, handle, 'Record it.\nCALL:record|{"note":"once"}')

    assert "recorded:once" in reply
    # The side effect happened exactly once, though the segment after it ran twice.
    assert ledger.read_text().split() == ["once"]

    history = await handle.fetch_history()
    scheduled = {}
    retried = []
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            scheduled[event.event_id] = attrs.activity_type.name
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = event.activity_task_started_event_attributes
            if attrs.attempt > 1:
                retried.append(scheduled[attrs.scheduled_event_id])
    assert retried == [CODEX_RUN_SEGMENT_ACTIVITY]

    # The model saw the real tool output exactly once, never Codex's synthetic "aborted".
    final_input = fake.requests[-1]["input"]
    outputs = [i["output"] for i in final_input if i.get("type") == "function_call_output"]
    assert outputs == ["recorded:once"]


@requires_codex_binary
async def test_subagents_are_harness_child_workflows(stack):
    """Codex's own multi_agent is off; subagents come from the harness: each is a separate child
    workflow the parent drives through ordinary host tools (start_/ask/stop_)."""
    client, task_queue, fake, _ledger = stack
    handle = await _start(client, task_queue, CodexParentAgent)
    script = (
        "Delegate this.\n"
        "CALL:start_helper|{}\n"
        'CALL:helper_ask|{"subagent":"{{out:0}}","q":{"text":"ping"}}\n'
        'CALL:stop_helper|{"subagent":"{{out:0}}"}'
    )
    events, reply = await _turn(client, handle, script)

    assert "child says: ping" in reply
    tool_ends = {
        e.event.tool_name: e.event.tool_output
        for e in events
        if e.event.type == AgentEventType.TOOL_END
    }
    assert set(tool_ends) == {"start_helper", "helper_ask", "stop_helper"}
    assert "child says: ping" in tool_ends["helper_ask"]

    # The subagent is a real child workflow of the parent, not a Codex-internal thread.
    history = await handle.fetch_history()
    children = [
        e.start_child_workflow_execution_initiated_event_attributes.workflow_type.name
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]
    assert children == ["EchoChildAgent"]
    assert "multi_agent_v1" not in _tool_names_offered(fake)
