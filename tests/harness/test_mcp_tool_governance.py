# ABOUTME: End-to-end tests that an MCP tool call gets the same governance as any other
# harness tool, with no wiring by the agent author. Run against the Temporal time-skipping
# test server, because the mechanism only exists in a real workflow: run() records the
# runner on the workflow instance, and AgentWorkflowRunner.current() is what an MCP call
# -- which never enters run_tool -- uses to reach the approval policy and turn stream.
#
# Covers:
#   * a gated MCP call waits for a human decision, then brackets with tool_start/tool_end;
#   * a denied MCP call never reaches the server and returns an is_error result;
#   * a policy that skips approvals still publishes the lifecycle bracket;
#   * the lifecycle events carry the SDK call id, so they join the model's tool_requested.
#
# The probe MCP server is duck-typed rather than an agents.mcp.MCPServer: governance only
# needs call_tool + tool_meta_resolver, and this keeps the workflow free of SDK imports.
#
# Run with: uv run pytest tests/harness/test_mcp_tool_governance.py -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
import pytest_asyncio
from mcp import types
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.openai_agents_harness import as_harness_mcp_server
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent import ToolApprovalPolicy
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_workflow import _RUNNER_ATTR
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

CALL_ID = "call_mcp_1"
TOOL_NAME = "probe_mcp_tool"


class _ProbeMCPServer:
    """The MCP-server surface governance touches, and nothing else."""

    tool_meta_resolver: Any = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        self.calls.append((tool_name, arguments, meta))
        text = (arguments or {}).get("text", "")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"mcp:{text}")]
        )


class _ProbeToolContext:
    """Stands in for the SDK ToolContext the meta resolver reads the call id off."""

    def __init__(self, tool_call_id: str) -> None:
        self.tool_call_id = tool_call_id


class _ProbeMetaContext:
    def __init__(self, tool_call_id: str) -> None:
        self.run_context = _ProbeToolContext(tool_call_id)


class _BaseMCPProbe:
    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Call a governed MCP server the way the OpenAI Agents SDK does."""
        server = cast("Any", as_harness_mcp_server(cast("Any", _ProbeMCPServer())))
        # The SDK resolves the request _meta, then calls call_tool with it.
        meta = await server.tool_meta_resolver(_ProbeMetaContext(CALL_ID))
        result = await server.call_tool(TOOL_NAME, {"text": message.text}, meta=meta)
        self._server_calls = server.calls
        block = result.content[0]
        assert isinstance(block, types.TextContent)
        return TextReply(text=block.text)


@workflow.defn
@agent.defn
class GatedMCPProbeAgent(_BaseMCPProbe):
    """Safe-by-default: every tool call is gated, MCP calls included."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
        )
        self._server_calls: list[Any] = []

    @workflow.query
    def server_call_count(self) -> int:
        return len(self._server_calls)

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


@workflow.defn
@agent.defn
class UngatedMCPProbeAgent(_BaseMCPProbe):
    """Approvals skipped, so the call runs straight through — the bracket must still
    be published."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        self._server_calls: list[Any] = []

    @workflow.query
    def server_call_count(self) -> int:
        return len(self._server_calls)

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)


@pytest_asyncio.fixture
async def env_and_client():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"mcp-governance-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[GatedMCPProbeAgent, UngatedMCPProbeAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str, workflow_cls: type):
    return await client.start_workflow(
        workflow_cls.run,
        AgentConfig(),
        id=f"{workflow_cls.__name__}-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _send(handle, text: str) -> None:
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="act", payload={"text": text}, expected_turn=1),
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


def _types_for(events: list[AgentEvent], tool_id: str) -> list[str]:
    return [e.event.type for e in events if getattr(e.event, "tool_id", None) == tool_id]


def _reply_text(events: list[AgentEvent]) -> str:
    reply = next(e.event for e in events if e.event.type == AgentEventType.REPLY)
    return reply.output["text"]


async def _run_turn(client: Client, handle, *, approve: bool | None) -> list[AgentEvent]:
    """Drive one turn, answering the approval prompt if one arrives."""
    agent_client = AgentClient(client, handle.id)
    events: list[AgentEvent] = []
    async with asyncio.timeout(30):
        async for item in _subscribe(client, handle.id):
            ev = item.data
            events.append(ev)
            if (
                approve is not None
                and ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
                and ev.event.tool_id == CALL_ID
            ):
                await agent_client.approve_tool(
                    CALL_ID, approved=approve, reason=None if approve else "not allowed"
                )
            if ev.event.type == AgentEventType.TURN_END:
                break
    return events


async def test_mcp_call_is_gated_then_brackets(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, GatedMCPProbeAgent)
    await _send(handle, "hello")

    events = await _run_turn(client, handle, approve=True)

    # The same lifecycle a harness tool gets, keyed by the SDK call id -- so these join
    # the tool_requested the model stream published under that id.
    assert _types_for(events, CALL_ID) == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    requested = next(
        e.event for e in events if e.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED
    )
    assert requested.tool_name == TOOL_NAME
    assert requested.tool_input == {"text": "hello"}
    end = next(e.event for e in events if e.event.type == AgentEventType.TOOL_END)
    assert end.tool_output == "mcp:hello"
    assert _reply_text(events) == "mcp:hello"
    assert await handle.query(GatedMCPProbeAgent.server_call_count) == 1


async def test_denied_mcp_call_never_reaches_the_server(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, GatedMCPProbeAgent)
    await _send(handle, "hello")

    events = await _run_turn(client, handle, approve=False)

    # Denied: resolved(approved=False), no start/end -- the server was never called.
    assert _types_for(events, CALL_ID) == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
    ]
    assert "was not approved" in _reply_text(events)
    assert await handle.query(GatedMCPProbeAgent.server_call_count) == 0


async def test_ungated_mcp_call_still_publishes_the_bracket(env_and_client):
    client, task_queue = env_and_client
    handle = await _start(client, task_queue, UngatedMCPProbeAgent)
    await _send(handle, "hello")

    events = await _run_turn(client, handle, approve=None)

    assert _types_for(events, CALL_ID) == [
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]
    assert _reply_text(events) == "mcp:hello"


# ---------------------------------------------------------------------------
# Runner resolution — how a call that never enters run_tool finds the agent
# ---------------------------------------------------------------------------


class _HostWorkflow:
    """Stands in for an agent workflow instance after run() recorded the runner."""

    def __init__(self, runner: object) -> None:
        self.some_other_attr = object()
        setattr(self, _RUNNER_ATTR, runner)


def test_current_reads_the_runner_run_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not an AgentWorkflowRunner: under the sandbox the agent's runner can come from a
    # second copy of this module, so the lookup must not isinstance-check.
    runner = object()
    instance = _HostWorkflow(runner)
    monkeypatch.setattr(workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(workflow, "instance", lambda: instance)

    assert AgentWorkflowRunner.current() is runner


def test_current_is_none_when_the_workflow_is_not_a_harness_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(workflow, "instance", lambda: object())

    assert AgentWorkflowRunner.current() is None


def test_current_is_none_outside_a_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "in_workflow", lambda: False)

    assert AgentWorkflowRunner.current() is None
