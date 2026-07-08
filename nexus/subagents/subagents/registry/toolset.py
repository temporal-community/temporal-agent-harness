# ABOUTME: registry_subagent_toolset() — the dynamic-discovery sibling of the harness's
# subagent_toolset(). Unlike subagent_toolset(), this doesn't know which agent(s) it's wiring
# ahead of time — it discovers them at runtime via the agent registry. Because the agent keys
# and their handler schemas aren't known until then, the generated tools are necessarily
# generic verbs (discover / start / send / stop) rather than one strongly-typed tool per
# handler. Lives here (not in the harness) because dynamic discovery is inherently a Nexus-only
# concept — there is no same-cluster child-workflow equivalent of "a directory of agents I
# don't know about yet."

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from temporal_agent_harness.harness.agent import tool_defn
from temporal_agent_harness.harness.agent_workflow import _current_runner

from .discovery import discover_registry_agents, start_subagent_from_registry


def _make_discover_tool(*, registry_endpoint: str) -> Callable[..., Awaitable[list[dict]]]:
    """Build ``discover_subagents``: the current agent-registry directory, tool-style."""

    async def _discover() -> list[dict[str, Any]]:
        agents = await discover_registry_agents(registry_endpoint)
        return [a.model_dump(mode="json") for a in agents]

    _discover.__name__ = "discover_subagents"
    _discover.__qualname__ = _discover.__name__
    _discover.__doc__ = (
        "List every agent currently registered with the agent registry — each entry's "
        "agent_key, description, and its callable handlers (name/description/input+output "
        "schema). Call this before start_subagent to see what's available and how to call it."
    )
    _discover.__signature__ = inspect.Signature([], return_annotation=list[dict])  # type: ignore[attr-defined]
    _discover.__annotations__ = {"return": list[dict]}
    return tool_defn()(_discover)


def _make_registry_start_tool(*, registry_endpoint: str) -> Callable[..., Awaitable[str]]:
    """Build ``start_subagent``: start an instance of a registry-discovered agent by key."""

    async def _start(agent_key: str) -> str:
        return await start_subagent_from_registry(_current_runner(), agent_key, registry_endpoint)

    _start.__name__ = "start_subagent"
    _start.__qualname__ = _start.__name__
    _start.__doc__ = (
        "Start an instance of the agent named `agent_key` (one returned by "
        "discover_subagents) and return its short handle. Pass that handle to "
        "send_subagent_message to drive it, and to stop_subagent to shut it down."
    )
    _start.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "agent_key", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            )
        ],
        return_annotation=str,
    )
    _start.__annotations__ = {"agent_key": str, "return": str}
    return tool_defn()(_start)


def _make_registry_send_tool() -> Callable[..., Awaitable[dict]]:
    """Build ``send_subagent_message``: generic dispatch (function name + raw payload dict),
    since the handler schema isn't known statically the way subagent_toolset()'s per-handler
    tools are — the model reads it from discover_subagents' output instead."""

    async def _send(
        subagent: str, function: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await _current_runner().run_subagent_turn(subagent, function, payload)

    _send.__name__ = "send_subagent_message"
    _send.__qualname__ = _send.__name__
    _send.__doc__ = (
        "Send a message to the subagent identified by `subagent` (the handle returned by "
        "start_subagent). `function` must be one of that agent's handler names (from "
        "discover_subagents); `payload` must match that handler's input schema. Returns the "
        "handler's raw JSON reply."
    )
    _send.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "subagent", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            ),
            inspect.Parameter(
                "function", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            ),
            inspect.Parameter(
                "payload", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=dict
            ),
        ],
        return_annotation=dict,
    )
    _send.__annotations__ = {
        "subagent": str,
        "function": str,
        "payload": dict,
        "return": dict,
    }
    return tool_defn()(_send)


def _make_registry_stop_tool() -> Callable[..., Awaitable[str]]:
    """Build ``stop_subagent``: stop a registry-discovered instance — identical mechanics to
    subagent_toolset()'s stop_<key>, just without the per-key namespacing since the set of
    keys isn't known ahead of time here."""

    async def _stop(subagent: str) -> str:
        await _current_runner().stop_subagent(subagent)
        return f"stopped subagent {subagent!r}"

    _stop.__name__ = "stop_subagent"
    _stop.__qualname__ = _stop.__name__
    _stop.__doc__ = (
        "Stop the subagent identified by `subagent` (the handle returned by start_subagent). "
        "Use it when that instance's work is done."
    )
    _stop.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(
                "subagent", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str
            )
        ],
        return_annotation=str,
    )
    _stop.__annotations__ = {"subagent": str, "return": str}
    return tool_defn()(_stop)


def registry_subagent_toolset(registry_endpoint: str) -> list[Callable[..., Awaitable[Any]]]:
    """Convert the agent registry into a toolset a parent agent can use to discover and drive
    ARBITRARY subagents at runtime — the dynamic-discovery sibling of ``agent.subagent_toolset``.

    Generates four fixed, generically-named tools (not one namespaced set per wired agent),
    because the agent keys and handler schemas aren't known until ``discover_subagents`` is
    actually called:

        * ``discover_subagents()``                          — list the current directory.
        * ``start_subagent(agent_key)``                     — start a discovered agent by key.
        * ``send_subagent_message(subagent, function, payload)`` — generic dispatch.
        * ``stop_subagent(subagent)``                        — stop an instance.

    Args:
        registry_endpoint: the registered Nexus endpoint fronting the agent registry.
    """
    return [
        _make_discover_tool(registry_endpoint=registry_endpoint),
        _make_registry_start_tool(registry_endpoint=registry_endpoint),
        _make_registry_send_tool(),
        _make_registry_stop_tool(),
    ]
