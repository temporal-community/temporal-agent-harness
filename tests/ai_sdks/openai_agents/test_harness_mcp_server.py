# ABOUTME: Unit-tests as_harness_mcp_server -- the wrapper that puts an OpenAI Agents SDK
# MCP server under harness tool governance. Exercises the two things MCP tools were missing:
# the approval gate, and the tool_start/tool_end(/tool_error) lifecycle events the UI needs
# to resolve a tool card. Also pins the call-id correlation: the SDK never hands call_tool
# the call id, so the wrapper routes it through the server's tool_meta_resolver and strips
# it back out before the call is forwarded.
#
# No Temporal server and no OPENAI_API_KEY: the runner is faked and the gate is patched.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_harness_mcp_server.py -v

from __future__ import annotations

from typing import Any, cast

import pytest
from agents.mcp import MCPServer
from mcp import types

from temporal_agent_harness.ai_sdks import openai_agents_harness as h
from temporal_agent_harness.harness.agent_protocol import (
    AgentEventType,
    ToolEndEvent,
    ToolErrorEvent,
    ToolStartEvent,
)
from temporal_agent_harness.harness.agent_workflow import (
    AgentWorkflowRunner,
    ToolApprovalDenied,
    _CURRENT_TOOL_ID,
)

CALL_ID = "call_abc123"


class _FakeRunner:
    """The bits of AgentWorkflowRunner the wrapper touches.

    ``run_tool`` borrows the real implementation, which only parks the ambient tool id /
    runner / injections -- so the test sees exactly the funnel the wrapper relies on.
    """

    def __init__(self) -> None:
        self.published: list[Any] = []
        self.run_tool_ids: list[str] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)

    async def run_tool(self, call_id: str, tool_callable: Any, /, **kwargs: Any) -> Any:
        self.run_tool_ids.append(call_id)
        return await AgentWorkflowRunner.run_tool(self, call_id, tool_callable, **kwargs)  # type: ignore[arg-type]


class _FakeMCPServer(MCPServer):  # type: ignore[misc]
    """An MCP server that records its calls and returns a canned result."""

    def __init__(self, result: types.CallToolResult | Exception, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._result = result
        self.calls: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = []

    @property
    def name(self) -> str:
        return "fake-mcp"

    async def connect(self) -> None: ...

    async def cleanup(self) -> None: ...

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[Any]:
        return []

    async def list_prompts(self) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> types.GetPromptResult:
        raise NotImplementedError

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        self.calls.append((tool_name, arguments, meta))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeToolContext:
    """Stands in for the SDK ToolContext the meta resolver reads the call id off."""

    def __init__(self, tool_call_id: str) -> None:
        self.tool_call_id = tool_call_id


class _FakeMetaContext:
    def __init__(self, tool_call_id: str) -> None:
        self.run_context = _FakeToolContext(tool_call_id)


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


def _first_text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _FakeRunner:
    """A fake runner, installed as the one AgentWorkflowRunner.current() resolves to."""
    fake = _FakeRunner()
    monkeypatch.setattr(
        AgentWorkflowRunner, "current", staticmethod(lambda: cast("Any", fake))
    )
    return fake


@pytest.fixture
def gate_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every approval-gate call and auto-approve. Also captures the ambient tool
    id, which is what proves the call went through ``run_tool``."""
    recorded: list[dict[str, Any]] = []

    async def _gate(tool_name: str, tool_input: dict[str, Any], *, inherently_safe: bool) -> None:
        recorded.append(
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "inherently_safe": inherently_safe,
                "ambient_tool_id": _CURRENT_TOOL_ID.get(),
            }
        )

    monkeypatch.setattr(h, "_apply_approval_policy", _gate)
    return recorded


async def _call_through(
    server: MCPServer,
    runner: _FakeRunner,
    *,
    tool_name: str = "get_forecast",
    arguments: dict[str, Any] | None = None,
    call_id: str | None = CALL_ID,
) -> types.CallToolResult:
    """Drive one call the way the SDK does: resolve the meta, then call_tool with it."""
    meta: dict[str, Any] | None = None
    if call_id is not None:
        resolver = cast("Any", server.tool_meta_resolver)
        meta = await resolver(_FakeMetaContext(call_id))
    return await server.call_tool(tool_name, arguments or {"city": "Oslo"}, meta=meta)


async def test_successful_call_brackets_with_start_and_end(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:
    server = h.as_harness_mcp_server(_FakeMCPServer(_text_result("sunny")))

    result = await _call_through(server, runner)

    assert _first_text(result) == "sunny"
    start, end = runner.published
    assert isinstance(start, ToolStartEvent)
    assert start.type is AgentEventType.TOOL_START
    assert start.tool_id == CALL_ID
    assert start.tool_name == "get_forecast"
    assert start.tool_input == {"city": "Oslo"}
    assert isinstance(end, ToolEndEvent)
    assert end.tool_id == CALL_ID
    assert end.tool_output == "sunny"


async def test_call_id_correlates_and_never_reaches_the_server(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:
    # The SDK does not pass the call id to call_tool, so the wrapper routes it through
    # the meta resolver. It must come back out as the tool_id and must NOT be forwarded.
    inner = _FakeMCPServer(_text_result("sunny"))
    server = h.as_harness_mcp_server(inner)

    await _call_through(server, runner)

    assert runner.run_tool_ids == [CALL_ID]
    assert gate_calls[0]["ambient_tool_id"] == CALL_ID
    _, _, forwarded_meta = inner.calls[0]
    assert forwarded_meta is None


async def test_existing_meta_resolver_is_chained_and_forwarded(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:

    def caller_resolver(_context: Any) -> dict[str, Any]:
        return {"tenant": "acme"}

    inner = _FakeMCPServer(_text_result("sunny"), tool_meta_resolver=caller_resolver)
    server = h.as_harness_mcp_server(inner)

    await _call_through(server, runner)

    _, _, forwarded_meta = inner.calls[0]
    assert forwarded_meta == {"tenant": "acme"}


async def test_missing_call_id_still_publishes_a_bracket(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:
    # No meta resolver run (e.g. an SDK that never calls it): the events fall back to a
    # fresh id rather than being dropped.
    server = h.as_harness_mcp_server(_FakeMCPServer(_text_result("sunny")))

    await _call_through(server, runner, call_id=None)

    start, end = runner.published
    assert start.tool_id and start.tool_id == end.tool_id
    assert start.tool_id != CALL_ID


async def test_error_result_publishes_tool_error(gate_calls: list[dict[str, Any]], runner: _FakeRunner) -> None:
    server = h.as_harness_mcp_server(_FakeMCPServer(_text_result("upstream 500", is_error=True)))

    await _call_through(server, runner)

    start, error = runner.published
    assert isinstance(start, ToolStartEvent)
    assert isinstance(error, ToolErrorEvent)
    assert error.tool_id == CALL_ID
    assert error.message == "upstream 500"


async def test_raising_server_publishes_tool_error_and_reraises(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:
    server = h.as_harness_mcp_server(_FakeMCPServer(RuntimeError("transport down")))

    with pytest.raises(RuntimeError, match="transport down"):
        await _call_through(server, runner)

    start, error = runner.published
    assert isinstance(start, ToolStartEvent)
    assert isinstance(error, ToolErrorEvent)
    assert error.message == "transport down"


async def test_denied_call_never_runs_and_returns_an_error_result(
    monkeypatch: pytest.MonkeyPatch, runner: _FakeRunner
) -> None:
    async def _deny(tool_name: str, tool_input: dict[str, Any], *, inherently_safe: bool) -> None:
        raise ToolApprovalDenied(tool_name, "nope")

    monkeypatch.setattr(h, "_apply_approval_policy", _deny)
    inner = _FakeMCPServer(_text_result("sunny"))
    server = h.as_harness_mcp_server(inner)

    result = await _call_through(server, runner)

    assert result.is_error is True
    assert "was not approved" in _first_text(result)
    assert inner.calls == []
    # The gate already published tool_approval_resolved; a call that never ran gets no
    # start/error bracket of its own.
    assert runner.published == []


async def test_inherently_safe_is_passed_to_the_gate(gate_calls: list[dict[str, Any]], runner: _FakeRunner) -> None:
    server = h.as_harness_mcp_server(_FakeMCPServer(_text_result("sunny")), inherently_safe=True)

    await _call_through(server, runner)

    assert gate_calls[0]["inherently_safe"] is True


async def test_as_harness_mcp_servers_wraps_each(gate_calls: list[dict[str, Any]], runner: _FakeRunner) -> None:
    servers = h.as_harness_mcp_servers(
        [_FakeMCPServer(_text_result("a")), _FakeMCPServer(_text_result("b"))]
    )

    for server in servers:
        await _call_through(server, runner)

    assert [type(e).__name__ for e in runner.published] == [
        "ToolStartEvent",
        "ToolEndEvent",
        "ToolStartEvent",
        "ToolEndEvent",
    ]


async def test_sdk_invocation_path_carries_the_call_id(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:
    # Pins the SDK contract the correlation rests on: MCPUtil.invoke_mcp_tool calls the
    # server's tool_meta_resolver with the ToolContext, then passes the resolved meta to
    # call_tool. If a future SDK drops that, this fails instead of the tool cards.
    from agents.mcp.util import MCPUtil
    from agents.run_context import RunContextWrapper
    from agents.tool_context import ToolContext

    inner = _FakeMCPServer(_text_result("sunny"))
    server = h.as_harness_mcp_server(inner)
    tool = types.Tool(
        name="get_forecast",
        description="fake",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
    )
    context = ToolContext.from_agent_context(
        RunContextWrapper(context=runner),
        CALL_ID,
        tool_name="get_forecast",
        tool_arguments='{"city": "Oslo"}',
    )

    await MCPUtil.invoke_mcp_tool(server, tool, context, '{"city": "Oslo"}')

    assert runner.run_tool_ids == [CALL_ID]
    start, end = runner.published
    assert start.tool_id == CALL_ID
    assert end.tool_id == CALL_ID
    assert inner.calls[0][2] is None


async def test_wrapping_twice_is_a_no_op(gate_calls: list[dict[str, Any]], runner: _FakeRunner) -> None:
    server = h.as_harness_mcp_server(_FakeMCPServer(_text_result("sunny")))
    assert h.as_harness_mcp_server(server) is server

    await _call_through(server, runner)

    # One bracket, not two nested ones.
    assert [type(e).__name__ for e in runner.published] == ["ToolStartEvent", "ToolEndEvent"]


async def test_call_outside_a_harness_agent_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The plugin can be used without a harness agent. There is no policy to apply and no
    # turn stream to publish to, so the call is forwarded untouched.
    monkeypatch.setattr(AgentWorkflowRunner, "current", staticmethod(lambda: None))
    inner = _FakeMCPServer(_text_result("sunny"))
    server = h.as_harness_mcp_server(inner)

    result = await server.call_tool("get_forecast", {"city": "Oslo"})

    assert _first_text(result) == "sunny"
    assert inner.calls == [("get_forecast", {"city": "Oslo"}, None)]


def test_every_mcp_server_factory_returns_a_governed_server() -> None:
    # Governance is applied at construction, so an agent author passes the result
    # straight to Agent(mcp_servers=[...]).
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import NexusGateway
    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        nexus_native_mcp_server,
        stateful_mcp_server,
        stateless_mcp_server,
    )

    built = {
        "stateless": stateless_mcp_server("probe"),
        "stateful": stateful_mcp_server("probe"),
        "nexus_native": nexus_native_mcp_server("demo-nexus", "endpoint"),
        "gateway": NexusGateway("agent-1").mcp_servers("demo"),
    }

    assert {name: h.is_harness_mcp_server(s) for name, s in built.items()} == {
        name: True for name in built
    }


def test_every_factory_forwards_inherently_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import NexusGateway
    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        nexus_native_mcp_server,
        stateful_mcp_server,
        stateless_mcp_server,
    )

    seen: list[bool] = []
    real = h.as_harness_mcp_server

    def spy(server: Any, *, inherently_safe: bool = False) -> Any:
        seen.append(inherently_safe)
        return real(server, inherently_safe=inherently_safe)

    # The factories import this by name at call time, so patching the module works.
    monkeypatch.setattr(h, "as_harness_mcp_server", spy)

    stateless_mcp_server("probe", inherently_safe=True)
    stateful_mcp_server("probe", inherently_safe=True)
    nexus_native_mcp_server("demo-nexus", "endpoint", inherently_safe=True)
    NexusGateway("agent-1").mcp_servers("demo", inherently_safe=True)

    assert seen == [True, True, True, True]


def test_ungoverned_mcp_server_is_rejected_before_the_run() -> None:
    # The factories are the only supported construction path, so the run fails loudly
    # rather than starting with an ungated server.
    from agents import Agent

    from temporal_agent_harness.ai_sdks.openai_agents._model_parameters import (
        ModelActivityParameters,
    )
    from temporal_agent_harness.ai_sdks.openai_agents._openai_runner import (
        TemporalOpenAIRunner,
    )
    from temporal_agent_harness.ai_sdks.openai_agents._mcp import (
        _StatelessMCPServerReference,
    )

    raw = _StatelessMCPServerReference("probe", None, False, None)
    agent = Agent(name="starting", model="gpt-5.1", mcp_servers=[raw])
    runner = TemporalOpenAIRunner(ModelActivityParameters())

    with pytest.raises(ValueError, match="not built by a harness mcp_server factory"):
        runner._prepare_workflow_run(agent, {})
