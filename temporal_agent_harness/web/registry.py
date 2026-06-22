"""Registry loading helpers for the harness web API."""

from __future__ import annotations

import tomllib
from pathlib import Path

from temporal_agent_harness.web.session_manager import AgentDescriptor, AgentRegistry

_REQUIRED_FIELDS = ("key", "workflow_type", "task_queue", "label", "description")


def load_agent_registry(path: Path | str) -> AgentRegistry:
    """Parse an agent registry TOML file into an :class:`AgentRegistry`."""

    registry_path = Path(path)
    with registry_path.open("rb") as file:
        raw = tomllib.load(file)

    raw_agents = raw.get("agents") or []
    if not raw_agents:
        raise ValueError(f"Agent registry {registry_path} defines no agents.")

    agents: list[AgentDescriptor] = []
    seen_keys: set[str] = set()
    for entry in raw_agents:
        missing = [field for field in _REQUIRED_FIELDS if not entry.get(field)]
        if missing:
            raise ValueError(
                f"Agent entry {entry!r} in {registry_path} is missing required "
                f"field(s): {missing}"
            )
        if entry["key"] in seen_keys:
            raise ValueError(f"Duplicate agent key {entry['key']!r} in {registry_path}.")
        seen_keys.add(entry["key"])
        agents.append(
            AgentDescriptor(
                key=entry["key"],
                workflow_type=entry["workflow_type"],
                task_queue=entry["task_queue"],
                label=entry["label"],
                description=" ".join(str(entry["description"]).split()),
            )
        )

    return AgentRegistry(agents=agents)
