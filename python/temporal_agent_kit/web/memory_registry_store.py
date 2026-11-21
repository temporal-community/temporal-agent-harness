"""A registry store in process memory, for tests.

It is lost when the process exits and is not shared between processes, so a server that should
keep its registry uses another store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING

from temporal_agent_kit.web.registry_store import (
    RegistrySnapshot,
    StaleRegistry,
    VersionedSnapshot,
)

if TYPE_CHECKING:
    from temporalio.client import Client


class InMemoryRegistryStore:
    """An :class:`~temporal_agent_kit.web.registry_store.AgentRegistryStore` holding its document
    in this object."""

    def __init__(self, snapshot: RegistrySnapshot | None = None) -> None:
        self._stored = (
            VersionedSnapshot()
            if snapshot is None
            else VersionedSnapshot(snapshot=snapshot, version=1)
        )
        self._lock = asyncio.Lock()

    async def read(self) -> VersionedSnapshot:
        return self._stored.model_copy(deep=True)

    async def write(self, snapshot: RegistrySnapshot, expected: int) -> int:
        async with self._lock:
            if self._stored.version != expected:
                raise StaleRegistry(
                    f"The registry is at version {self._stored.version}, not {expected}."
                )
            self._stored = VersionedSnapshot(
                snapshot=snapshot.model_copy(deep=True), version=expected + 1
            )
            return self._stored.version


def in_memory_registry_store(
    store: InMemoryRegistryStore | None = None,
) -> Callable[[Client], AbstractAsyncContextManager[InMemoryRegistryStore]]:
    """A factory for ``store``, or for a new empty store, that ignores the Temporal client."""

    @asynccontextmanager
    async def factory(_client: Client) -> AsyncIterator[InMemoryRegistryStore]:
        yield store if store is not None else InMemoryRegistryStore()

    return factory
