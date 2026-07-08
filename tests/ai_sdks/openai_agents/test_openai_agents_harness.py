# ABOUTME: Tests for as_openai_agent_tool's dynamic-schema seam (_DYNAMIC_SCHEMA_ATTR) — lets a
# Nexus-discovered tool (no Python type, just a JSON Schema) get a real params_json_schema
# instead of an empty one from introspecting its `**kwargs` signature. Uses a duck-typed fake
# runner, not a real AgentWorkflowRunner — only run_tool() is ever called.

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    _DYNAMIC_SCHEMA_ATTR,
    as_openai_agent_tool,
)
from temporal_agent_harness.harness.agent import tool_defn


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = []

    async def run_tool(self, call_id, tool_callable, /, *args, injections=None, **kwargs):
        self.calls.append((call_id, tool_callable, args, kwargs))
        return {"echo": kwargs}


def _make_dynamic_tool() -> Any:
    """Mirrors nexus/subagents' registry.toolset.as_tool(): a tool_defn()-wrapped callable
    whose real signature is an uninformative **kwargs, with the real schema stashed on
    _DYNAMIC_SCHEMA_ATTR instead."""

    async def _call(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    _call.__name__ = "echo_ask"
    _call.__qualname__ = "echo_ask"
    _call.__doc__ = "Ask the echo subagent to echo some text."
    _call.__signature__ = inspect.Signature(
        [inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD, annotation=Any)],
        return_annotation=dict,
    )
    tool = tool_defn()(_call)
    setattr(
        tool,
        _DYNAMIC_SCHEMA_ATTR,
        {
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            "output": {"type": "object"},
        },
    )
    return tool


def test_dynamic_schema_tool_uses_stashed_schema_not_introspection():
    tool = as_openai_agent_tool(_FakeRunner(), _make_dynamic_tool(), strict_json_schema=False)
    assert tool.name == "echo_ask"
    assert tool.description == "Ask the echo subagent to echo some text."
    assert tool.params_json_schema == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }


@pytest.mark.asyncio
async def test_dynamic_schema_tool_invocation_passes_raw_kwargs_through():
    runner = _FakeRunner()
    tool = as_openai_agent_tool(runner, _make_dynamic_tool(), strict_json_schema=False)

    result = await tool.on_invoke_tool(_FakeContext(), json.dumps({"text": "hello"}))

    assert len(runner.calls) == 1
    call_id, tool_callable, args, kwargs = runner.calls[0]
    assert args == ()
    assert kwargs == {"text": "hello"}
    assert result == json.dumps({"echo": {"text": "hello"}})


@pytest.mark.asyncio
async def test_dynamic_schema_tool_rejects_non_object_input():
    from temporalio.exceptions import ApplicationError

    tool = as_openai_agent_tool(_FakeRunner(), _make_dynamic_tool(), strict_json_schema=False)
    with pytest.raises(ApplicationError, match="InvalidToolInput"):
        await tool.on_invoke_tool(_FakeContext(), json.dumps(["not", "an", "object"]))


class _FakeContext:
    tool_call_id = "call-1"
