from __future__ import annotations

import inspect
import json
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest
from temporalio.contrib.openai_agents import OpenAIPayloadConverter
from temporalio.converter import DataConverter

from temporal_agent_harness.ai_sdks import openai_agents_plugin
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent import Injected
from temporal_agent_harness.harness.agent_protocol import AgentEvent, TurnStarted
from temporal_agent_harness.harness.agent_workflow import _event_with_discriminator_set


@dataclass
class _FakeSchema:
    fn: Any

    @property
    def name(self) -> str:
        return self.fn.__name__

    @property
    def description(self) -> str:
        return inspect.getdoc(self.fn) or ""

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                name: {}
                for name in inspect.signature(self.fn).parameters
            },
        }

    def params_pydantic_model(self, **data: Any) -> Any:
        return types.SimpleNamespace(data=data)

    def to_call_args(self, parsed: Any) -> tuple[list[Any], dict[str, Any]]:
        bound = inspect.signature(self.fn).bind(**parsed.data)
        bound.apply_defaults()
        return [], dict(bound.arguments)


class _FakeFunctionTool:
    def __init__(
        self,
        *,
        name: str,
        description: str,
        params_json_schema: dict[str, Any],
        on_invoke_tool: Any,
        strict_json_schema: bool,
    ) -> None:
        self.name = name
        self.description = description
        self.params_json_schema = params_json_schema
        self.on_invoke_tool = on_invoke_tool
        self.strict_json_schema = strict_json_schema


class _FakeRunner:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result if result is not None else {"ok": True}
        self.error = error

    async def run_tool(
        self,
        call_id: str,
        tool_callable: Any,
        /,
        *args: Any,
        injections: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "call_id": call_id,
                "tool_callable": tool_callable,
                "args": args,
                "kwargs": kwargs,
                "injections": injections,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake_agents_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    agents_mod = types.ModuleType("agents")
    function_schema_mod = types.ModuleType("agents.function_schema")
    tool_mod = types.ModuleType("agents.tool")
    function_schema_mod.function_schema = lambda fn: _FakeSchema(fn)
    tool_mod.FunctionTool = _FakeFunctionTool
    monkeypatch.setitem(sys.modules, "agents", agents_mod)
    monkeypatch.setitem(sys.modules, "agents.function_schema", function_schema_mod)
    monkeypatch.setitem(sys.modules, "agents.tool", tool_mod)


@agent.activity_tool_defn()
async def sample_search(
    store: Injected[str], page_url: str, limit: int = 5
) -> str:
    """Search one page."""
    return f"{store}:{page_url}:{limit}"


@agent.tool_defn()
async def inline_tool(text: str) -> str:
    """Echo text."""
    return text


def test_importing_openai_agents_plugin_does_not_require_optional_dependency() -> None:
    assert openai_agents_plugin.__all__


async def test_openai_payload_converter_decodes_published_harness_events() -> None:
    converter = DataConverter(payload_converter_class=OpenAIPayloadConverter)
    event = AgentEvent(
        agent_id="agent",
        turn_id="turn",
        turn_number=1,
        timestamp=1.0,
        event=_event_with_discriminator_set(TurnStarted(user_message="hello")),
    )

    payloads = await converter.encode([event])
    decoded = await converter.decode(payloads, [AgentEvent])

    assert isinstance(decoded[0], AgentEvent)
    assert isinstance(decoded[0].event, TurnStarted)


def test_as_openai_agent_tool_uses_model_facing_signature(
    fake_agents_modules: None,
) -> None:
    runner = _FakeRunner()

    tool = openai_agents_plugin.as_openai_agent_tool(
        runner, sample_search, injections={"store": "docs"}
    )

    assert tool.name == "sample_search"
    assert tool.description == "Search one page."
    assert set(tool.params_json_schema["properties"]) == {"page_url", "limit"}
    assert tool.strict_json_schema is True


async def test_as_openai_agent_tool_invokes_runner_run_tool(
    fake_agents_modules: None,
) -> None:
    runner = _FakeRunner()
    tool = openai_agents_plugin.as_openai_agent_tool(
        runner, sample_search, injections={"store": "docs"}
    )

    output = await tool.on_invoke_tool(
        object(),
        json.dumps({"page_url": "https://example.com", "limit": 2}),
    )

    assert json.loads(output) == {"ok": True}
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["call_id"].startswith("openai_agents:sample_search:")
    assert call["tool_callable"] is sample_search
    assert call["args"] == ()
    assert call["kwargs"] == {"page_url": "https://example.com", "limit": 2}
    assert call["injections"] == {"store": "docs"}


async def test_as_openai_agent_tool_uses_sdk_tool_call_id(
    fake_agents_modules: None,
) -> None:
    runner = _FakeRunner()
    tool = openai_agents_plugin.as_openai_agent_tool(runner, inline_tool)

    ctx = types.SimpleNamespace(tool_call_id="call_123")
    await tool.on_invoke_tool(ctx, json.dumps({"text": "hello"}))

    assert runner.calls[0]["call_id"] == "call_123"


async def test_as_openai_agent_tool_returns_model_visible_tool_error(
    fake_agents_modules: None,
) -> None:
    runner = _FakeRunner(error=RuntimeError("approval denied"))
    tool = openai_agents_plugin.as_openai_agent_tool(runner, inline_tool)

    output = await tool.on_invoke_tool(object(), json.dumps({"text": "hello"}))

    assert output == "Tool 'inline_tool' failed: approval denied"


async def test_as_openai_agent_tool_rejects_invalid_json(
    fake_agents_modules: None,
) -> None:
    runner = _FakeRunner()
    tool = openai_agents_plugin.as_openai_agent_tool(runner, inline_tool)

    with pytest.raises(Exception) as excinfo:
        await tool.on_invoke_tool(object(), "not-json")

    assert "Invalid JSON input" in str(excinfo.value)
    assert runner.calls == []


def test_as_openai_agent_tool_rejects_plain_functions(
    fake_agents_modules: None,
) -> None:
    async def plain_tool(text: str) -> str:
        return text

    with pytest.raises(TypeError, match="not a harness tool"):
        openai_agents_plugin.as_openai_agent_tool(_FakeRunner(), plain_tool)
