"""Where the server keeps its agent registry: a versioned document behind a pluggable store.

The registry is one document, :class:`RegistrySnapshot`. A store keeps it under a version and
replaces it only if the version has not moved since it was read; the rules for changing it live
in :mod:`temporal_agent_kit.web.registry_changes`, which every store shares. A store is built by a
:data:`RegistryStoreFactory` the server calls with its Temporal client.

This module imports nothing beyond pydantic and the standard library, so a workflow can hold the
document.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field, JsonValue

if TYPE_CHECKING:
    from temporalio.client import Client


@dataclass
class AgentStart:
    """What an agent takes to start, after its config: its ``@agent.init`` init data."""

    # {"required": bool, "schema": {...}}: the init data's JSON Schema, self-contained for a form
    # (agent_init_data_schema). ``None`` when the agent takes no init data.
    init_data: dict[str, Any] | None = None


class RegisteredAgent(BaseModel):
    """One agent type in the registry, as it was registered."""

    key: str
    workflow_type: str
    # Without TASK_QUEUE_PREFIX: the server puts it under its prefix when it reads the entry.
    task_queue: str
    label: str
    description: str
    # The agent's whole schema document (`temporal-agent-kit schema`), or None when it was
    # registered without one.
    schema_document: dict[str, JsonValue] | None = None
    # Its init data, extracted from ``schema_document`` when the entry was registered. ``None``
    # when there is no schema document, so the agent's start interface is unknown.
    start: AgentStart | None = None
    retired: bool = False


class RegistrySnapshot(BaseModel):
    """The whole registry: every agent type, by key."""

    agents: dict[str, RegisteredAgent] = Field(default_factory=dict)

    @classmethod
    def of(cls, agents: Iterable[RegisteredAgent]) -> RegistrySnapshot:
        return cls(agents={agent.key: agent for agent in agents})


class VersionedSnapshot(BaseModel):
    """A snapshot and the version a store holds it under."""

    snapshot: RegistrySnapshot = Field(default_factory=RegistrySnapshot)
    # 0 when nothing has been written.
    version: int = 0


class RegistryWrite(BaseModel):
    """A write: store ``snapshot`` as the next version if the stored one is still ``expected``."""

    snapshot: RegistrySnapshot
    expected: int


class StaleRegistry(Exception):
    """The stored version is no longer the one a write expected."""


class AgentRegistryStore(Protocol):
    """Holds the registry document under a version."""

    async def read(self) -> VersionedSnapshot:
        """The stored snapshot and its version: an empty snapshot at version 0 when nothing has
        been written."""
        ...

    async def write(self, snapshot: RegistrySnapshot, expected: int) -> int:
        """Store ``snapshot`` as version ``expected + 1`` if the stored version is ``expected``,
        and return the new version. Raise :class:`StaleRegistry` otherwise."""
        ...


RegistryStoreFactory = Callable[["Client"], AbstractAsyncContextManager[AgentRegistryStore]]
"""Builds a store from the server's Temporal client. The server enters it in its lifespan, so a
store can start what it needs (a worker, a connection pool) and stop it with the server."""
