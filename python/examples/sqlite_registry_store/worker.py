"""Worker for the reference SQLite registry store: the AgentKitRegistryStore Nexus service.

Run from the repo root with:
    bazel run //python/examples/sqlite_registry_store:worker -- --db "$PWD/agent-registry.sqlite3"

A server keeps its agent registry here when started with
``temporal-agent-kit serve --registry-store-endpoint <endpoint>``, where the Nexus endpoint
targets this worker's task queue (see README.md).

Connects like the examples' workers: a temporal.toml profile plus TEMPORAL_* env vars.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from temporal_agent_kit.web.client import (
    TemporalConnection,
    connect_client,
    ensure_temporal_connection,
)

from examples.sqlite_registry_store.store import SqliteRegistryStore

TASK_QUEUE = "agent-kit-registry-store"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=Path("agent-registry.sqlite3"), help="The SQLite database file.")
    parser.add_argument("--task-queue", default=TASK_QUEUE, help=f"The task queue to poll (default: {TASK_QUEUE}).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    ensure_temporal_connection(TemporalConnection())
    # The server's data converter, so this worker reads the snapshots it writes; the worker runs on
    # a client without the Agent Kit plugin, which would add agent activities to it.
    client = await connect_client()
    worker_client = Client(
        client.service_client, namespace=client.namespace, data_converter=client.data_converter
    )
    store = SqliteRegistryStore(args.db.resolve())
    logging.info(
        "AgentKitRegistryStore ready: namespace=%s taskQueue=%s db=%s",
        client.namespace,
        args.task_queue,
        store.path,
    )
    await Worker(worker_client, task_queue=args.task_queue, nexus_service_handlers=[store]).run()


if __name__ == "__main__":
    asyncio.run(main())
