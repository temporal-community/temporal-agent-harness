"""The Nexus registry store: the server keeps its registry in the AgentKitRegistryStore Nexus
service behind an endpoint, here the reference SQLite store (examples/sqlite_registry_store).

Runs on a local dev server, which serves standalone Nexus operations.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.sqlite_registry_store.store import SqliteRegistryStore
from temporal_agent_kit.web import app as app_module
from temporal_agent_kit.web.nexus_registry_store import (
    NexusRegistryStore,
    RegistryStoreError,
    nexus_registry_store,
    registry_store_schema,
)
from temporal_agent_kit.web.registry_changes import change_registry, registered
from temporal_agent_kit.web.registry_store import (
    RegisteredAgent,
    RegistrySnapshot,
    StaleRegistry,
    VersionedSnapshot,
)
from temporal_agent_kit.web.serve import create_app

# Relative to the runfiles root the test starts in.
CONTRACT = json.loads(Path("contract/registry-store.schema.json").read_text())

_DEV_SERVER_ARGS = [
    # Standalone Nexus operations, which the server calls the store with, are off by default.
    "--dynamic-config-value",
    "nexusoperation.enableStandalone=true",
    # Endpoint registration is cached; 0s keeps create_nexus_endpoint visible immediately.
    "--dynamic-config-value",
    'system.system.refreshNexusEndpointsMinWait="0s"',
]

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter, dev_server_extra_args=_DEV_SERVER_ARGS
    )
    yield env
    await env.shutdown()


@pytest_asyncio.fixture(loop_scope="module")
async def endpoint(env: WorkflowEnvironment, tmp_path: Path) -> AsyncIterator[str]:
    """An endpoint of its own, served by a SQLite store of its own."""
    name = f"registry-{uuid.uuid4().hex[:12]}"
    task_queue = f"{name}-queue"
    await env.create_nexus_endpoint(name, task_queue)
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[SqliteRegistryStore(tmp_path / "registry.sqlite3")],
    ):
        yield name


def _agent(key: str) -> RegisteredAgent:
    return RegisteredAgent(
        key=key,
        workflow_type=f"{key.title()}Agent",
        task_queue=f"{key}-queue",
        label=key.title(),
        description=f"The {key} agent.",
    )


async def test_the_committed_contract_is_what_the_server_generates() -> None:
    assert registry_store_schema() == CONTRACT


async def test_a_store_nothing_has_written_reads_as_empty(env: WorkflowEnvironment, endpoint: str) -> None:
    async with asyncio.timeout(30):
        assert await NexusRegistryStore(env.client, endpoint).read() == VersionedSnapshot()


async def test_writes_are_read_back_under_their_version(env: WorkflowEnvironment, endpoint: str) -> None:
    store = NexusRegistryStore(env.client, endpoint)
    async with asyncio.timeout(30):
        assert await store.write(RegistrySnapshot.of([_agent("a")]), 0) == 1
        stored = await store.read()

    assert stored.version == 1
    assert list(stored.snapshot.agents) == ["a"]


async def test_a_write_from_a_moved_version_is_stale(env: WorkflowEnvironment, endpoint: str) -> None:
    store = NexusRegistryStore(env.client, endpoint)
    async with asyncio.timeout(30):
        await store.write(RegistrySnapshot.of([_agent("a")]), 0)
        with pytest.raises(StaleRegistry, match="at version 1, not 0"):
            await store.write(RegistrySnapshot.of([_agent("b")]), 0)
        assert list((await store.read()).snapshot.agents) == ["a"]


async def test_concurrent_changes_from_two_servers_both_land(env: WorkflowEnvironment, endpoint: str) -> None:
    one, two = NexusRegistryStore(env.client, endpoint), NexusRegistryStore(env.client, endpoint)
    async with asyncio.timeout(60):
        await asyncio.gather(
            *(
                change_registry(store, lambda s, key=key: registered(s, [_agent(key)]))
                for store, key in ((one, "a"), (two, "b"), (one, "c"), (two, "d"))
            )
        )
        assert sorted((await one.read()).snapshot.agents) == ["a", "b", "c", "d"]


async def test_an_endpoint_that_is_not_there_stops_the_server_as_it_starts(env: WorkflowEnvironment) -> None:
    async with asyncio.timeout(30):
        with pytest.raises(RegistryStoreError, match="no-such-endpoint"):
            async with nexus_registry_store("no-such-endpoint")(env.client):
                pass


async def test_serve_registers_its_files_into_the_store_behind_the_endpoint(
    env: WorkflowEnvironment, endpoint: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def connect(**_config: Any) -> Client:
        return env.client

    monkeypatch.setattr(app_module.ClientConfig, "load_client_connect_config", lambda: {})
    monkeypatch.setattr(app_module.Client, "connect", connect)
    registry = tmp_path / "agents.toml"
    registry.write_text(
        "[[agents]]\n"
        'key = "hello"\n'
        'workflow_type = "HelloAgent"\n'
        'task_queue = "hello"\n'
        'label = "Hello"\n'
        'description = "Says hello."\n'
    )

    app = create_app(registry, registry_store_endpoint=endpoint)
    async with asyncio.timeout(30):
        async with app.router.lifespan_context(app):
            pass
        stored = await NexusRegistryStore(env.client, endpoint).read()

    assert list(stored.snapshot.agents) == ["hello"]
