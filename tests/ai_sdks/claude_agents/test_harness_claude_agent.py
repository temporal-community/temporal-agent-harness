# ABOUTME: End-to-end tests of HarnessClaudeAgent on the Temporal test server, with the PR's
# ScriptedClaude standing in for Claude (no engine, no API key). Pins that the harness, not the
# plugin, owns approvals and tool lifecycle events for BOTH kinds of call the plugin makes:
# harness tools (custom) and Claude Code's own Bash (an "engine" call run as its own Activity).
#
# Run with: uv run --extra claude-agent-sdk pytest tests/ai_sdks/claude_agents -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio

pytest.importorskip("temporalio.claude_agent_sdk")

from temporalio import workflow  # noqa: E402
from temporalio.claude_agent_sdk import ClaudeAgentPlugin  # noqa: E402
from temporalio.claude_agent_sdk.testing import (  # noqa: E402
    Final,
    HistoryItem,
    ScriptedClaude,
    ToolCall,
)
from temporalio.contrib.pydantic import pydantic_data_converter  # noqa: E402
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import UnsandboxedWorkflowRunner, Worker  # noqa: E402

from temporal_agent_harness.ai_sdks.claude_agent_sdk_harness import (  # noqa: E402
    HarnessClaudeAgent,
    as_claude_tool,
)
from temporal_agent_harness.harness import AgentWorkflowRunner, agent  # noqa: E402
from temporal_agent_harness.harness.agent import ToolApprovalPolicy  # noqa: E402
from temporal_agent_harness.harness.agent_client import AgentClient  # noqa: E402
from temporal_agent_harness.harness.agent_protocol import (  # noqa: E402
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    TextMessage,
    TextReply,
)

BASH_RAN: list[dict] = []


@agent.tool_defn(inherently_safe=True)
async def get_weather(city: str, units: str = "f") -> str:
    """Return the weather for a city."""
    return f"sunny in {city} ({units})"


def _policy(prompt: str, history: list[HistoryItem]):
    """get_weather first, then Bash (if the prompt asks), then answer with what Claude saw."""
    done = {h.name for h in history}
    if "get_weather" not in done:
        return ToolCall("get_weather", {"city": "Paris"})
    if "bash" in prompt and "Bash" not in done:
        return ToolCall("Bash", {"command": "echo hi"})
    seen = "|".join(f"{h.name}={h.content}:{'err' if h.is_error else 'ok'}" for h in history)
    return Final(seen)


def _bash(inp: dict) -> dict:
    BASH_RAN.append(inp)
    return "hi"


@agent.defn
class ClaudeProbeAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            # Safe by default: get_weather is inherently safe, Bash is not.
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._claude = HarnessClaudeAgent(
            self._runner,
            tools=[get_weather],
            builtin_tools=["Bash"],
            segment_timeout=timedelta(seconds=30),
        )

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Run Claude on the message text."""
        return TextReply(text=await self._claude.run(message.text))


@pytest_asyncio.fixture
async def env_and_queue():
    BASH_RAN.clear()
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"claude-harness-{uuid.uuid4()}"
    runner = ScriptedClaude(_policy, engine_tools={"Bash": _bash})
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ClaudeProbeAgent],
        plugins=[ClaudeAgentPlugin(runner)],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _ask(client, task_queue: str, text: str, *, on_event=None) -> tuple[str, list[AgentEvent]]:
    handle = await client.start_workflow(
        ClaudeProbeAgent.run,
        AgentConfig(),
        id=f"claude-probe-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    update = asyncio.ensure_future(
        handle.execute_update(
            SEND_AGENT_MESSAGE_UPDATE,
            AgentMessage(type="act", payload={"text": text}),
        )
    )
    agent_client = AgentClient(client, handle.id)
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id).subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    )
    async with asyncio.timeout(60):
        async for item in stream:
            ev = item.data
            events.append(ev)
            if on_event is not None:
                await on_event(agent_client, ev)
            if ev.event.type == AgentEventType.TURN_END:
                break
        await update
    reply = next(e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END)
    return reply.output["text"], events


def _types(events: list[AgentEvent], name: str) -> list[str]:
    return [e.event.type for e in events if getattr(e.event, "tool_name", None) == name]


def test_as_claude_tool_describes_the_model_facing_signature():
    tool = as_claude_tool(get_weather)
    assert tool.name == "get_weather"
    assert tool.description == "Return the weather for a city."
    assert tool.needs_approval is False
    assert tool.input_schema["required"] == ["city"]
    assert set(tool.input_schema["properties"]) == {"city", "units"}


def test_as_claude_tool_rejects_a_plain_function():
    async def plain(x: str) -> str:
        return x

    with pytest.raises(TypeError, match="not a harness tool"):
        as_claude_tool(plain)


def test_plugin_approvals_are_refused():
    with pytest.raises(ValueError, match="approvals are the harness's"):
        HarnessClaudeAgent(None, tool_approvals=["Bash"])  # type: ignore[arg-type]


async def test_harness_tool_runs_ungated_with_lifecycle_events(env_and_queue):
    client, task_queue = env_and_queue
    text, events = await _ask(client, task_queue, "weather please")
    assert text.startswith("get_weather=sunny in Paris (f):ok")
    assert _types(events, "get_weather") == [AgentEventType.TOOL_START, AgentEventType.TOOL_END]
    assert not BASH_RAN


async def test_bash_waits_for_the_harness_approval_then_runs(env_and_queue):
    client, task_queue = env_and_queue

    async def approve(agent_client: AgentClient, ev: AgentEvent) -> None:
        if ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
            assert ev.event.tool_name == "Bash"
            assert ev.event.tool_input == {"command": "echo hi"}
            assert not BASH_RAN, "Bash must not run before it is approved"
            await agent_client.approve_tool(ev.event.tool_id, approved=True)

    text, events = await _ask(client, task_queue, "bash please", on_event=approve)
    assert BASH_RAN == [{"command": "echo hi"}]
    assert text.endswith("Bash=hi:ok")
    assert _types(events, "Bash") == [
        AgentEventType.TOOL_APPROVAL_REQUESTED,
        AgentEventType.TOOL_APPROVAL_RESOLVED,
        AgentEventType.TOOL_START,
        AgentEventType.TOOL_END,
    ]


async def test_denied_bash_never_runs_and_claude_sees_the_rejection(env_and_queue):
    client, task_queue = env_and_queue

    async def deny(agent_client: AgentClient, ev: AgentEvent) -> None:
        if ev.event.type == AgentEventType.TOOL_APPROVAL_REQUESTED:
            await agent_client.approve_tool(ev.event.tool_id, approved=False, reason="no")

    text, events = await _ask(client, task_queue, "bash please", on_event=deny)
    assert not BASH_RAN
    assert "Bash=A human reviewer rejected this action. Do not retry it.:err" in text
    # A denied call never starts: no start/end/error bracket.
    assert AgentEventType.TOOL_START not in _types(events, "Bash")


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------


def test_schema_survives_an_annotation_pydantic_cannot_resolve():
    @agent.tool_defn(inherently_safe=True)
    async def odd(city: "NotImportable", n: int = 1) -> str:  # type: ignore[name-defined]  # noqa: F821
        """Takes a type nobody can resolve."""
        return city

    schema = as_claude_tool(odd).input_schema
    assert set(schema["properties"]) == {"city", "n"}
    assert schema["properties"]["n"]["type"] == "integer"


def test_a_harness_tool_may_not_share_a_name_with_a_claude_code_tool():
    @agent.tool_defn()
    async def Bash(command: str) -> str:  # noqa: N802
        """Shadows Claude Code's Bash."""
        return command

    with pytest.raises(ValueError, match="same name as the Claude Code tool"):
        HarnessClaudeAgent(None, tools=[Bash], builtin_tools=["Bash"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="same name as the Claude Code tool"):
        HarnessClaudeAgent(None, tools=[Bash])  # type: ignore[arg-type]  # default tool_activities


async def test_a_failing_engine_tool_closes_its_tool_start(monkeypatch):
    import temporal_agent_harness.ai_sdks.claude_agent_sdk_harness as mod

    published: list = []

    class FakeRunner:
        def publish(self, event):
            published.append(event)

        async def run_tool(self, call_id, fn, *a, **kw):
            return await fn()

    async def allow(*a, **kw):
        return None

    async def boom(self, call, record):
        raise asyncio.CancelledError()

    monkeypatch.setattr(mod, "_apply_approval_policy", allow)
    monkeypatch.setattr(mod.DurableClaudeAgent, "_execute_tool", boom)
    claude = HarnessClaudeAgent(FakeRunner())  # type: ignore[arg-type]
    call = mod.DeferredCall(id="t1", name="Bash", input={"command": "x"}, kind="engine")
    with pytest.raises(asyncio.CancelledError):
        await claude._execute_tool(call, {})
    assert [type(e).__name__ for e in published] == ["ToolStartEvent", "ToolErrorEvent"]
