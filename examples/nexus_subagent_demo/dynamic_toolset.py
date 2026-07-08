"""Prototype: turn a Nexus agent-registry directory into REAL, individually-typed model
tools at runtime, instead of one generic ``send_subagent_message(subagent, function,
payload)`` dispatch tool (``registry_subagent_toolset`` in ``nexus/subagents/registry``).

Deliberately built OUTSIDE the harness and OUTSIDE ``nexus/subagents/*`` too — everything
here goes through public surface (``subagents.registry.discover_registry_agents``/
``start_subagent_from_registry``, ``AgentWorkflowRunner.run_subagent_turn``, the harness's
public ``agent.tool_defn``); nothing here reaches into a private symbol of either package.
That's deliberate: it's a live test of whether this class of feature needs to live inside
either package at all, or whether their existing public surface is already sufficient to
build it externally.

Two pieces:

* :func:`as_tool` — converts one discovered ``HandlerElement`` into a tool wrapping a single
  subagent turn. Built from the handler's ACTUAL JSON schema (fetched live over Nexus), not a
  statically-known Python type — there is no pydantic class to introspect the way the static
  ``subagent_toolset`` generator's ``_make_send_tool`` does, so the schema is attached
  directly (see ``tool_declaration``) rather than derived via ``inspect.Signature``. Reuses a
  running instance per ``agent_key`` across calls via a ``cache`` dict the CALLER owns (so it
  persists across turns) — the model never has to track a handle at all.
* :func:`discover_and_build_tools` — the "force discovery every turn" half: always re-fetches
  the live directory and rebuilds the tool list from it, so a parent's model never sees (or
  needs to call) a ``discover_subagents`` tool itself — the harness does that for it, before
  the model ever sees a tool list.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from temporal_agent_harness.harness.agent import tool_defn
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

from subagents.registry import HandlerElement, discover_registry_agents, start_subagent_from_registry

# Stashed on each synthesized tool by as_tool() — the raw JSON Schema fetched over Nexus,
# read back out by tool_declaration() instead of being re-derived via signature introspection
# (there is no Python type here for inspect.Signature/FunctionDeclaration.from_callable to
# introspect in the first place).
_SCHEMA_ATTR = "_nexus_schema"


def as_tool(
    *,
    agent_key: str,
    handler: HandlerElement,
    registry_endpoint: str,
    runner: AgentWorkflowRunner,
    cache: dict[str, str],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Build one tool — named ``<agent_key>_<handler.name>`` — wrapping a single subagent
    turn against ``handler``.

    Reuses a running instance of ``agent_key`` across calls: ``start_subagent_from_registry``
    always starts a FRESH instance (same as ``start_subagent`` — there is no starter-side
    "start or reuse" behavior), so reuse has to be done here, keyed by ``agent_key`` in
    ``cache`` — a plain dict the CALLER owns and passes to every ``as_tool``/
    ``discover_and_build_tools`` call this parent makes, so it persists across turns instead
    of resetting every time the tool list is rebuilt."""

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
    # tool_defn() wraps _call in a NEW function object (for approval-gating/lifecycle-event
    # plumbing) and returns THAT wrapper — a custom attribute set on _call before wrapping
    # does NOT carry over (only __name__/__doc__/__signature__/__annotations__ do, via
    # tool_defn's own _apply_model_facing_views). So stash the schema on the wrapper it
    # actually returns, not on _call.
    tool = tool_defn()(_call)
    setattr(tool, _SCHEMA_ATTR, {"parameters": handler.parameters, "output": handler.output})
    return tool


async def discover_and_build_tools(
    runner: AgentWorkflowRunner, registry_endpoint: str, cache: dict[str, str]
) -> list[Callable[..., Awaitable[dict[str, Any]]]]:
    """Force a fresh ``discover_agents`` call (never cached across turns — an agent that
    registered or deregistered since the last turn is picked up immediately) and synthesize
    one :func:`as_tool` per discovered handler, across every currently-registered agent.

    ``cache`` is the same agent_key->handle dict every call this parent makes should share —
    see :func:`as_tool`'s docstring."""
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
    """The model-facing tool declaration for a tool built by :func:`as_tool` — what would be
    handed to ``tools=`` on a real model call (Gemini/OpenAI-shaped: ``{type, name,
    description, parameters}``).

    Deliberately does NOT go through the harness's ``function_param`` (the Gemini plugin's
    callable-introspection adapter) — that adapter's whole job is deriving a JSON Schema FROM a
    real Python type on the callable's signature, which doesn't exist here (see the module
    docstring). We already have the real schema; this just reads it back out."""
    schema = getattr(fn, _SCHEMA_ATTR)
    return {
        "type": "function",
        "name": fn.__name__,
        "description": inspect.cleandoc(fn.__doc__) if fn.__doc__ else "",
        "parameters": schema["parameters"],
    }
