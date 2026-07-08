# ABOUTME: discover_and_build_tools()/as_tool() — dynamic-discovery sibling of the harness's
# subagent_toolset(). Discovers agents at runtime via the registry and synthesizes one real,
# typed tool per handler (built from its live JSON schema) instead of one generic dispatch tool.
# Lives here (not the harness) since dynamic discovery is Nexus-only. Supersedes an earlier
# generic 4-verb toolset (discover/start/send/stop) outright.

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from temporal_agent_harness.harness.agent import tool_defn
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

from .agent_registry_service import HandlerElement
from .discovery import discover_registry_agents, start_subagent_from_registry

# Stashed on each synthesized tool — the JSON Schema fetched over Nexus (there's no Python type
# to introspect). Public so an AI-SDK adapter can read it directly (see
# ai_sdks.openai_agents_harness.as_openai_agent_tool).
SCHEMA_ATTR = "_nexus_schema"


def as_tool(
    *,
    agent_key: str,
    handler: HandlerElement,
    registry_endpoint: str,
    runner: AgentWorkflowRunner,
    cache: dict[str, str],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build one tool named ``<agent_key>_<handler.name>``.

    ``start_subagent_from_registry`` always starts a fresh instance, so reuse is done here via
    ``cache`` (agent_key -> handle) — a dict the CALLER owns and shares across calls so it
    persists across turns."""

    async def _call(**kwargs: Any) -> dict[str, Any]:
        handle = cache.get(agent_key)
        if handle is None:
            handle = await start_subagent_from_registry(runner, agent_key, registry_endpoint)
            cache[agent_key] = handle
        return await runner.run_subagent_turn(handle, handler.name, kwargs)

    tool_name = f"{agent_key}_{handler.name}"
    _call.__name__ = tool_name
    _call.__qualname__ = tool_name
    _call.__doc__ = handler.description
    _call.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter("kwargs", inspect.Parameter.VAR_KEYWORD, annotation=Any)],
        return_annotation=dict,
    )
    # tool_defn() returns a NEW wrapper object; attributes on _call don't carry over, so stash
    # the schema on the wrapper it actually returns.
    tool = tool_defn()(_call)
    setattr(tool, SCHEMA_ATTR, {"parameters": handler.parameters, "output": handler.output})
    return tool


async def discover_and_build_tools(
    runner: AgentWorkflowRunner, registry_endpoint: str, cache: dict[str, str]
) -> list[Callable[..., Awaitable[dict[str, Any]]]]:
    """Fresh (uncached) discover_agents call, then one :func:`as_tool` per handler across every
    registered agent. ``cache`` should be shared across every call this parent makes."""
    agents = await discover_registry_agents(registry_endpoint)
    return [
        as_tool(
            agent_key=agent.agent_key,
            handler=handler,
            registry_endpoint=registry_endpoint,
            runner=runner,
            cache=cache,
        )
        for agent in agents
        for handler in agent.handlers
    ]


def tool_declaration(fn: Callable[..., Any]) -> dict[str, Any]:
    """Model-facing declaration for a tool built by :func:`as_tool` (Gemini/OpenAI-shaped:
    {type, name, description, parameters}), for callers driving a model API directly rather
    than through a harness ai_sdks adapter. Reads the schema stashed at ``SCHEMA_ATTR`` — no
    Python type to introspect here."""
    schema = getattr(fn, SCHEMA_ATTR)
    return {
        "type": "function",
        "name": fn.__name__,
        "description": inspect.cleandoc(fn.__doc__) if fn.__doc__ else "",
        "parameters": schema["parameters"],
    }
