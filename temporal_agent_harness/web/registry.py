"""Registry loading helpers for the harness web API."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
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


def load_agent_registries(paths: Iterable[Path | str]) -> AgentRegistry:
    """Load and merge several agent registry TOML files into one :class:`AgentRegistry`.

    Each file is parsed with :func:`load_agent_registry`, then the agents are concatenated. Both
    ``key`` and ``workflow_type`` must be unique across the merged set — ``key`` because the UI /
    ``by_key`` rely on it, and ``workflow_type`` because :meth:`AgentRegistry.by_workflow_type`
    (which session creation routes on) returns the first match, so a duplicate would silently
    shadow. Passing a single path yields the same registry as :func:`load_agent_registry`.
    """
    agents: list[AgentDescriptor] = []
    seen_keys: dict[str, Path] = {}
    seen_types: dict[str, Path] = {}
    for path in paths:
        registry_path = Path(path)
        for descriptor in load_agent_registry(registry_path).agents:
            if descriptor.key in seen_keys:
                raise ValueError(
                    f"Duplicate agent key {descriptor.key!r} across registries "
                    f"({seen_keys[descriptor.key]} and {registry_path})."
                )
            if descriptor.workflow_type in seen_types:
                raise ValueError(
                    f"Duplicate agent workflow_type {descriptor.workflow_type!r} across "
                    f"registries ({seen_types[descriptor.workflow_type]} and {registry_path})."
                )
            seen_keys[descriptor.key] = registry_path
            seen_types[descriptor.workflow_type] = registry_path
            agents.append(descriptor)

    if not agents:
        raise ValueError("No agent registries provided.")

    return AgentRegistry(agents=agents)
