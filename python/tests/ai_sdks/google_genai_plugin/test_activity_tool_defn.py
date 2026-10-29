# ABOUTME: Tests that the tool schema shown to the model (built by function_param over an
# @agent.activity_tool_defn tool) HIDES parameters annotated Injected[...], while keeping
# them in the activity's real call signature so the workflow can still supply them.
#
# Deliberately NO `from __future__ import annotations`: tools keep real annotation
# objects so the Gemini SDK's schema introspection (which runs with the harness module's
# globals) resolves them.
#
# Run with: uv run pytest tests/ai_sdks/google_genai_plugin/test_activity_tool_defn.py -v

import inspect
from typing import Any

import pytest

from temporal_agent_harness.ai_sdks.google_genai_plugin import function_param
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent import Injected


@agent.activity_tool_defn()
async def sample_tool(
    store: Injected[str], page_url: str, limit: int = 5
) -> str:
    """A probe tool: `store` is workflow-injected, the rest are model-facing."""
    return f"{store}:{page_url}:{limit}"


@agent.tool_defn()
async def inline_tool(text: str) -> str:
    """An inline (workflow) tool — has no activity to register."""
    return text


def _model_schema_props(tool: Any) -> set[str]:
    """The property names the model sees for ``tool`` (its function-call schema)."""
    # FunctionParam.parameters is typed ``object`` (an opaque JSON-schema blob), so
    # narrow to dict before indexing into it.
    params = function_param(tool).get("parameters")
    if not isinstance(params, dict):
        return set()
    return set(params.get("properties", {}))


def test_function_param_hides_injected_param_from_model_schema():
    props = _model_schema_props(sample_tool)
    # The Injected param must NOT be offered to the model...
    assert "store" not in props
    # ...while the ordinary parameters still are (and nothing else leaks in).
    assert props == {"page_url", "limit"}


def test_function_param_uses_the_tool_name():
    assert function_param(sample_tool)["name"] == "sample_tool"


def test_activity_tool_defn_dispatcher_signature_excludes_injected_param():
    # function_param introspects the dispatcher's model-facing signature, so the same
    # exclusion must hold there — the model-facing signature drops the injected parameter.
    params = list(inspect.signature(sample_tool).parameters)
    assert "store" not in params
    assert params == ["page_url", "limit"]


def test_activity_body_signature_keeps_injected_param_plus_tool_ctx():
    # The registrable activity (via tool_activity()) still receives the injected param
    # (the workflow supplies it) plus the trailing AgentToolContext the harness ferries in.
    params = list(inspect.signature(agent.tool_activity(sample_tool)).parameters)
    assert params == ["store", "page_url", "limit", "tool_ctx"]


def test_tool_activity_rejects_non_activity_tools():
    # An inline tool has no activity; a plain function was never decorated. Both raise.
    with pytest.raises(TypeError):
        agent.tool_activity(inline_tool)

    async def plain() -> None: ...

    with pytest.raises(TypeError):
        agent.tool_activity(plain)
