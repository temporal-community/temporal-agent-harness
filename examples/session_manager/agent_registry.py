# ABOUTME: Loads the agent registry (examples/session_manager/agents.toml) into the dataclasses the
# session manager consumes. This is the file-I/O half of the registry; it lives OUTSIDE
# the workflow module on purpose so the workflow sandbox never imports the file parser.
#
# Whatever process starts the SessionManagerWorkflow (the FastAPI server) calls
# load_agent_registry() and passes the result in as the manager's init arg. The manager
# then serves it back over its `available_agents` query.

from __future__ import annotations

import tomllib
from pathlib import Path

from examples.session_manager.workflow import AgentDescriptor, AgentRegistry

# Default location: alongside this module. Resolved relative to the source file (not the
# process CWD) so it loads identically from the web server, the CLI, or a test runner.
DEFAULT_REGISTRY_PATH = Path(__file__).parent / "agents.toml"

_REQUIRED_FIELDS = ("key", "workflow_type", "task_queue", "label", "description")


def load_agent_registry(path: Path | str | None = None) -> AgentRegistry:
    """Parse the agent registry TOML into an :class:`AgentRegistry`.

    Validates that every agent entry has the required fields and that keys are unique, so
    a malformed config fails loudly at startup rather than producing a manager that can't
    launch anything. Does not pick a default agent — that's a caller's decision.
    """
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    with registry_path.open("rb") as f:
        raw = tomllib.load(f)

    raw_agents = raw.get("agents") or []
    if not raw_agents:
        raise ValueError(f"Agent registry {registry_path} defines no agents.")

    agents: list[AgentDescriptor] = []
    seen_keys: set[str] = set()
    for entry in raw_agents:
        missing = [f for f in _REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            raise ValueError(
                f"Agent entry {entry!r} in {registry_path} is missing required "
                f"field(s): {missing}"
            )
        if entry["key"] in seen_keys:
            raise ValueError(
                f"Duplicate agent key {entry['key']!r} in {registry_path}."
            )
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
