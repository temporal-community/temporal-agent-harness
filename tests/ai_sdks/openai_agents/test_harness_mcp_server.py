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

from importlib.util import find_spec
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

# `nexus_native_mcp_server` and `NexusGateway` import `nexus_mcp` — an unpublished
# distribution resolved from nexus/mcp, so an environment built outside a checkout of this
# repo does not have it and those factories raise rather than build. Scoped to the tests
# that call them, NOT to the module: everything else here (the approval gate, the
# tool_start/tool_end bracket, the call-id correlation, the runner's durability check)
# needs nothing from nexus and has to keep running. Same line
# tests/ai_sdks/openai_agents/conftest.py draws.
requires_nexus_mcp = pytest.mark.skipif(
    find_spec("nexus_mcp") is None,
    reason="temporal-nexus-mcp is not installed (unpublished; resolves from nexus/mcp)",
)


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


def _govern(server: MCPServer, runner: _FakeRunner, **kwargs: Any) -> MCPServer:
    """as_harness_mcp_server with the fake runner cast into the real parameter type."""
    return h.as_harness_mcp_server(server, cast("AgentWorkflowRunner", runner), **kwargs)


@pytest.fixture
def runner() -> _FakeRunner:
    """The runner handed to as_harness_mcp_server, standing in for the agent's own."""
    return _FakeRunner()


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
    server = _govern(_FakeMCPServer(_text_result("sunny")), runner)

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
    server = _govern(inner, runner)

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
    server = _govern(inner, runner)

    await _call_through(server, runner)

    _, _, forwarded_meta = inner.calls[0]
    assert forwarded_meta == {"tenant": "acme"}


async def test_missing_call_id_still_publishes_a_bracket(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:
    # No meta resolver run (e.g. an SDK that never calls it): the events fall back to a
    # fresh id rather than being dropped.
    server = _govern(_FakeMCPServer(_text_result("sunny")), runner)

    await _call_through(server, runner, call_id=None)

    start, end = runner.published
    assert start.tool_id and start.tool_id == end.tool_id
    assert start.tool_id != CALL_ID


async def test_error_result_publishes_tool_error(gate_calls: list[dict[str, Any]], runner: _FakeRunner) -> None:
    server = _govern(_FakeMCPServer(_text_result("upstream 500", is_error=True)), runner)

    await _call_through(server, runner)

    start, error = runner.published
    assert isinstance(start, ToolStartEvent)
    assert isinstance(error, ToolErrorEvent)
    assert error.tool_id == CALL_ID
    assert error.message == "upstream 500"


async def test_raising_server_publishes_tool_error_and_reraises(
    gate_calls: list[dict[str, Any]], runner: _FakeRunner
) -> None:
    server = _govern(_FakeMCPServer(RuntimeError("transport down")), runner)

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
    server = _govern(inner, runner)

    result = await _call_through(server, runner)

    assert result.is_error is True
    assert "was not approved" in _first_text(result)
    assert inner.calls == []
    # The gate already published tool_approval_resolved; a call that never ran gets no
    # start/error bracket of its own.
    assert runner.published == []


async def test_inherently_safe_is_passed_to_the_gate(gate_calls: list[dict[str, Any]], runner: _FakeRunner) -> None:
    server = _govern(_FakeMCPServer(_text_result("sunny")), runner, inherently_safe=True)

    await _call_through(server, runner)

    assert gate_calls[0]["inherently_safe"] is True


async def test_as_harness_mcp_servers_wraps_each(gate_calls: list[dict[str, Any]], runner: _FakeRunner) -> None:
    servers = h.as_harness_mcp_servers(
        [_FakeMCPServer(_text_result("a")), _FakeMCPServer(_text_result("b"))],
        cast("Any", runner),
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
    server = _govern(inner, runner)
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
    server = _govern(_FakeMCPServer(_text_result("sunny")), runner)
    assert _govern(server, runner) is server

    await _call_through(server, runner)

    # One bracket, not two nested ones.
    assert [type(e).__name__ for e in runner.published] == ["ToolStartEvent", "ToolEndEvent"]


def _assert_governed_and_durable(built: dict[str, Any]) -> None:
    """Every factory result carries both marks the runner checks."""
    assert {name: h.is_harness_mcp_server(s) for name, s in built.items()} == {
        name: True for name in built
    }
    # The runner checks this mark. A factory that omits it builds a server the runner
    # rejects.
    assert {name: h.is_durable_mcp_server(s) for name, s in built.items()} == {
        name: True for name in built
    }


def test_every_mcp_server_factory_returns_a_governed_server() -> None:
    # Governance is applied at construction, so an agent author passes the result
    # straight to Agent(mcp_servers=[...]).
    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        stateful_mcp_server,
        stateless_mcp_server,
    )

    runner = cast("AgentWorkflowRunner", _FakeRunner())
    _assert_governed_and_durable(
        {
            "stateless": stateless_mcp_server("probe", runner=runner),
            "stateful": stateful_mcp_server("probe", runner=runner),
        }
    )


@requires_nexus_mcp
def test_every_nexus_mcp_server_factory_returns_a_governed_server() -> None:
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import NexusGateway
    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        nexus_native_mcp_server,
    )

    runner = cast("AgentWorkflowRunner", _FakeRunner())
    _assert_governed_and_durable(
        {
            "nexus_native": nexus_native_mcp_server("demo-nexus", "endpoint", runner=runner),
            "gateway": NexusGateway("agent-1").mcp_servers("demo", runner=runner),
        }
    )


def _spy_on_governance(monkeypatch: pytest.MonkeyPatch, runner: Any) -> list[tuple[bool, bool]]:
    """Record (runner matched, inherently_safe) for each as_harness_mcp_server call."""
    seen: list[tuple[bool, bool]] = []
    real = h.as_harness_mcp_server

    def spy(server: Any, got: Any, *, inherently_safe: bool = False) -> Any:
        seen.append((got is runner, inherently_safe))
        return real(server, got, inherently_safe=inherently_safe)

    # The factories import this by name at call time, so patching the module works.
    monkeypatch.setattr(h, "as_harness_mcp_server", spy)
    return seen


def test_every_factory_passes_runner_and_inherently_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        stateful_mcp_server,
        stateless_mcp_server,
    )

    runner = cast("AgentWorkflowRunner", _FakeRunner())
    seen = _spy_on_governance(monkeypatch, runner)

    stateless_mcp_server("probe", runner=runner, inherently_safe=True)
    stateful_mcp_server("probe", runner=runner, inherently_safe=True)

    assert seen == [(True, True)] * 2


@requires_nexus_mcp
def test_every_nexus_factory_passes_runner_and_inherently_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from temporal_agent_harness.ai_sdks.openai_agents._nexus_mcp import NexusGateway
    from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
        nexus_native_mcp_server,
    )

    runner = cast("AgentWorkflowRunner", _FakeRunner())
    seen = _spy_on_governance(monkeypatch, runner)

    nexus_native_mcp_server("demo-nexus", "endpoint", runner=runner, inherently_safe=True)
    NexusGateway("agent-1").mcp_servers("demo", runner=runner, inherently_safe=True)

    assert seen == [(True, True)] * 2


def _prepare_with(server: MCPServer) -> None:
    """Run the runner's pre-flight validation over one mcp_server."""
    from agents import Agent

    from temporal_agent_harness.ai_sdks.openai_agents._model_parameters import (
        ModelActivityParameters,
    )
    from temporal_agent_harness.ai_sdks.openai_agents._openai_runner import (
        TemporalOpenAIRunner,
    )

    TemporalOpenAIRunner(ModelActivityParameters())._prepare_workflow_run(
        Agent(name="probe", mcp_servers=[server]), {}
    )


def test_runner_rejects_an_unmarked_mcp_server() -> None:
    # A plain SDK server (stdio, HTTP) re-runs its tool calls on replay.
    with pytest.raises(ValueError, match="may not work durably"):
        _prepare_with(_FakeMCPServer(_text_result("sunny")))


def test_runner_rejects_a_marked_but_ungoverned_mcp_server() -> None:
    server = h.mark_durable_mcp_server(_FakeMCPServer(_text_result("sunny")))
    with pytest.raises(ValueError, match="not built by a harness mcp_server factory"):
        _prepare_with(server)


def test_runner_accepts_a_marked_and_governed_mcp_server(runner: _FakeRunner) -> None:
    server = _govern(h.mark_durable_mcp_server(_FakeMCPServer(_text_result("sunny"))), runner)
    _prepare_with(server)


def test_runner_validation_does_not_import_nexus_mcp() -> None:
    """Only the nexus factories may import nexus_mcp.

    Validation of a non-Nexus MCP server must not import it. Otherwise the harness
    cannot be released without temporal-nexus-mcp.
    """
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(
        """
        import sys
        from agents import Agent
        from temporal_agent_harness.ai_sdks.openai_agents._model_parameters import (
            ModelActivityParameters,
        )
        from temporal_agent_harness.ai_sdks.openai_agents._openai_runner import (
            TemporalOpenAIRunner,
        )
        from temporal_agent_harness.ai_sdks.openai_agents.workflow import (
            stateless_mcp_server,
        )

        server = stateless_mcp_server("probe", runner=None)
        TemporalOpenAIRunner(ModelActivityParameters())._prepare_workflow_run(
            Agent(name="probe", mcp_servers=[server]), {}
        )
        assert "nexus_mcp" not in sys.modules, sorted(
            m for m in sys.modules if "nexus" in m
        )
        print("clean")
        """
    )
    # A subprocess: another test in this session may already have imported nexus_mcp.
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout
