"""The rules for changing the agent registry, shared by every registry store.

Registering, retiring and deleting are pure functions from a snapshot to a new snapshot.
:func:`change_registry` applies one to a store: it reads the document, applies the change, and
writes it back with the version it read, reading again when another writer got there first.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from temporal_agent_kit.web.registry_store import (
    AgentRegistryStore,
    RegisteredAgent,
    RegistrySnapshot,
    StaleRegistry,
)

# A write that keeps losing to other writers gives up after this many reads.
CHANGE_ATTEMPTS = 5


class RegistryConflict(ValueError):
    """A change the registry's rules refuse, given what it holds."""


class UnknownAgent(LookupError):
    """A change to a key the registry does not hold."""

    def __init__(self, key: str) -> None:
        super().__init__(f"No agent {key!r} is registered.")
        self.key = key


class RegistryBusy(Exception):
    """A change that lost to other writers on every attempt."""


def registered(snapshot: RegistrySnapshot, agents: Sequence[RegisteredAgent]) -> RegistrySnapshot:
    """``snapshot`` with every one of ``agents`` added or replaced, active.

    Raises :class:`RegistryConflict` for a key or workflow type used twice in ``agents``, a
    workflow type another key holds, or a change to an existing key's ``workflow_type`` or
    ``task_queue``: that pair is how the server finds an agent's sessions, so changing it would
    hide them.
    """
    keys: set[str] = set()
    types: dict[str, str] = {}
    for agent in agents:
        if agent.key in keys:
            raise RegistryConflict(f"The agent key {agent.key!r} is given twice.")
        keys.add(agent.key)
        if agent.workflow_type in types:
            raise RegistryConflict(
                f"The workflow type {agent.workflow_type!r} is given for both "
                f"{types[agent.workflow_type]!r} and {agent.key!r}."
            )
        types[agent.workflow_type] = agent.key

    for existing in snapshot.agents.values():
        if existing.key in keys:
            continue
        if existing.workflow_type in types:
            raise RegistryConflict(
                f"The workflow type {existing.workflow_type!r} is already registered as "
                f"{existing.key!r}, so {types[existing.workflow_type]!r} cannot have it."
            )

    agents_by_key = dict(snapshot.agents)
    for agent in agents:
        existing = agents_by_key.get(agent.key)
        if existing is not None and (
            existing.workflow_type != agent.workflow_type or existing.task_queue != agent.task_queue
        ):
            raise RegistryConflict(
                f"{agent.key!r} is registered as {existing.workflow_type!r} on "
                f"{existing.task_queue!r}. Delete it before registering it as "
                f"{agent.workflow_type!r} on {agent.task_queue!r}, since its sessions are found "
                "by its workflow type and task queue."
            )
        agents_by_key[agent.key] = agent.model_copy(update={"retired": False})
    return RegistrySnapshot(agents=agents_by_key)


def retired(snapshot: RegistrySnapshot, key: str) -> RegistrySnapshot:
    """``snapshot`` with ``key`` retired. Raises :class:`UnknownAgent`."""
    agent = _agent(snapshot, key)
    return RegistrySnapshot(
        agents={**snapshot.agents, key: agent.model_copy(update={"retired": True})}
    )


def deleted(snapshot: RegistrySnapshot, key: str) -> RegistrySnapshot:
    """``snapshot`` without ``key``. Raises :class:`UnknownAgent`."""
    _agent(snapshot, key)
    return RegistrySnapshot(agents={k: a for k, a in snapshot.agents.items() if k != key})


async def change_registry(
    store: AgentRegistryStore, change: Callable[[RegistrySnapshot], RegistrySnapshot]
) -> RegistrySnapshot:
    """Apply ``change`` to the registry in ``store`` and return the registry it leaves.

    A change that leaves the registry as it was is not written. Raises what ``change`` raises,
    or :class:`RegistryBusy` when every attempt found the registry changed under it.
    """
    for _ in range(CHANGE_ATTEMPTS):
        current = await store.read()
        changed = change(current.snapshot)
        if changed == current.snapshot:
            return changed
        try:
            await store.write(changed, current.version)
        except StaleRegistry:
            continue
        return changed
    raise RegistryBusy(
        f"The registry changed under this write {CHANGE_ATTEMPTS} times in a row. Try again."
    )


def _agent(snapshot: RegistrySnapshot, key: str) -> RegisteredAgent:
    agent = snapshot.agents.get(key)
    if agent is None:
        raise UnknownAgent(key)
    return agent
