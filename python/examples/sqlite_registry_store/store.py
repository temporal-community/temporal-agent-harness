"""A reference AgentKitRegistryStore: the agent registry in one row of a SQLite table.

It implements the registry store's Nexus service (``contract/registry-store.nexusrpc.yaml``) for a
server started with ``serve --registry-store-endpoint``. A store keeps one JSON document under a
version and replaces it only from the version the writer read; the registry's rules run in the
server. SQLite makes the compare and the replace one transaction. This is a reference, not a
production store: one file, on the host the handler's worker runs on.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

from nexusrpc.handler import StartOperationContext, service_handler, sync_operation

from temporal_agent_kit.web.nexus_registry_store import (
    RegistryRead,
    RegistryStoreService,
    RegistryWriteOutcome,
)
from temporal_agent_kit.web.registry_store import RegistrySnapshot, RegistryWrite, VersionedSnapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_registry (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL,
    snapshot TEXT NOT NULL
)
"""


@service_handler(service=RegistryStoreService)
class SqliteRegistryStore:
    """The registry in the SQLite database at ``path``, created on first use."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(self._connect()) as db:
            db.execute(_SCHEMA)

    @sync_operation
    async def read(self, ctx: StartOperationContext, input: RegistryRead) -> VersionedSnapshot:
        return await asyncio.to_thread(self._read)

    @sync_operation
    async def write(self, ctx: StartOperationContext, input: RegistryWrite) -> RegistryWriteOutcome:
        return await asyncio.to_thread(self._write, input)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, isolation_level=None, timeout=10)

    def _read(self) -> VersionedSnapshot:
        with closing(self._connect()) as db:
            row = db.execute("SELECT version, snapshot FROM agent_registry WHERE id = 1").fetchone()
        if row is None:
            return VersionedSnapshot()
        version, snapshot = row
        return VersionedSnapshot(
            snapshot=RegistrySnapshot.model_validate_json(snapshot), version=version
        )

    def _write(self, write: RegistryWrite) -> RegistryWriteOutcome:
        db = self._connect()
        try:
            # IMMEDIATE takes the write lock now, so the version read below cannot move before
            # the update lands.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version FROM agent_registry WHERE id = 1").fetchone()
            stored = 0 if row is None else row[0]
            if stored != write.expected:
                db.execute("ROLLBACK")
                return RegistryWriteOutcome(stored=False, version=stored)
            db.execute(
                "INSERT INTO agent_registry (id, version, snapshot) VALUES (1, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET version = excluded.version, snapshot = excluded.snapshot",
                (stored + 1, write.snapshot.model_dump_json()),
            )
            db.execute("COMMIT")
            return RegistryWriteOutcome(stored=True, version=stored + 1)
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
