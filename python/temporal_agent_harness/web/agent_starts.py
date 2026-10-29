"""What each registered agent takes to start: its init data, from the registry.

A registry entry carries its agent's start interface (:class:`AgentStart`), which a TOML registry
reads from the agent's generated schema document (``schema = "X.schema.json"``). The web server
uses it to tell a caller whether an agent takes init data, whether it is required, and what shape
it has, and to refuse a start that sends data to an agent that takes none or omits data an agent
requires. Everything else about the data is the agent's to validate when it starts.
"""

from __future__ import annotations

from typing import Any

from temporal_agent_harness.web.session_manager import AgentRegistry, AgentStart


class InvalidStartData(ValueError):
    """A session start's ``data`` does not fit the agent's init data."""


def agent_starts(registry: AgentRegistry) -> dict[str, AgentStart]:
    """Each entry's start interface, keyed by workflow type. Entries without one are left out:
    their start interface is unknown."""
    return {
        descriptor.workflow_type: descriptor.start
        for descriptor in registry.agents
        if descriptor.start is not None
    }


def validated_start_data(
    workflow_type: str, start: AgentStart | None, data: dict[str, Any] | None
) -> dict[str, Any] | None:
    """``data``, once it is known to be present exactly when the agent needs it, or untouched
    when the agent's start interface is unknown. The agent validates its contents.

    Raises :class:`InvalidStartData` for data sent to an agent that takes none, or missing
    required data."""
    if start is None:
        return data
    init_data = start.init_data
    if init_data is None:
        if data is not None:
            raise InvalidStartData(f"{workflow_type} takes no init data.")
        return None
    if data is None and init_data["required"]:
        title = init_data["schema"].get("title")
        raise InvalidStartData(
            f"{workflow_type} requires init data" + (f" ({title})." if title else ".")
        )
    return data
