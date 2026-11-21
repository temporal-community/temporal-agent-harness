"""The registry store the server uses by default: the registry document in one workflow, served by
a worker inside the server."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio.client import WorkflowExecutionStatus, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Replayer

from temporal_agent_kit.web import registry_workflow
from temporal_agent_kit.web.registry_changes import change_registry, registered
from temporal_agent_kit.web.registry_store import (
    RegisteredAgent,
    RegistrySnapshot,
    StaleRegistry,
    VersionedSnapshot,
)
from temporal_agent_kit.web.registry_workflow import (
    AgentRegistryWorkflow,
    WorkflowRegistryStore,
    workflow_registry_store,
)

# A history of the registry workflow at REGISTRY_WORKFLOW_REVISION 1: two writes, then a
# continue-as-new to revision 2. A later revision of the workflow must still replay it.
REVISION_1_HISTORY = Path(__file__).parent / "registry_workflow_revision_1.history.json"

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter)
    yield env
    await env.shutdown()


@pytest.fixture
def prefix(monkeypatch: pytest.MonkeyPatch) -> str:
    """A TASK_QUEUE_PREFIX of the test's own, so each test has a registry of its own."""
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("TASK_QUEUE_PREFIX", prefix)
    return prefix


def _agent(key: str) -> RegisteredAgent:
    return RegisteredAgent(
        key=key,
        workflow_type=f"{key.title()}Agent",
        task_queue=f"{key}-queue",
        label=key.title(),
        description=f"The {key} agent.",
    )


async def test_a_registry_nothing_has_written_reads_as_empty_and_is_not_started(
    env: WorkflowEnvironment, prefix: str
) -> None:
    async with workflow_registry_store(env.client) as store:
        assert await store.read() == VersionedSnapshot()
    with pytest.raises(RPCError) as raised:
        await env.client.get_workflow_handle(f"{prefix}-agent-kit-registry").describe()
    assert raised.value.status == RPCStatusCode.NOT_FOUND


async def test_writes_are_read_back_under_their_version(env: WorkflowEnvironment, prefix: str) -> None:
    async with workflow_registry_store(env.client) as store:
        assert await store.write(RegistrySnapshot.of([_agent("a")]), 0) == 1
        assert await store.write(RegistrySnapshot.of([_agent("a"), _agent("b")]), 1) == 2

        stored = await store.read()

    assert stored.version == 2
    assert sorted(stored.snapshot.agents) == ["a", "b"]


async def test_a_stale_write_is_refused(env: WorkflowEnvironment, prefix: str) -> None:
    async with workflow_registry_store(env.client) as store:
        await store.write(RegistrySnapshot.of([_agent("a")]), 0)

        with pytest.raises(StaleRegistry):
            await store.write(RegistrySnapshot.of([_agent("b")]), 0)
        assert list((await store.read()).snapshot.agents) == ["a"]


async def test_concurrent_changes_from_two_servers_both_land(
    env: WorkflowEnvironment, prefix: str
) -> None:
    """Two server processes share one registry: one of them hosts the worker here, since a
    process may run only one worker on a task queue."""
    registry_id = f"{prefix}-agent-kit-registry"
    async with workflow_registry_store(env.client) as one:
        two = WorkflowRegistryStore(env.client, workflow_id=registry_id, task_queue=registry_id)
        await asyncio.gather(
            *(
                change_registry(store, lambda s, key=key: registered(s, [_agent(key)]))
                for store, key in ((one, "a"), (two, "b"), (one, "c"), (two, "d"))
            )
        )
        assert sorted((await one.read()).snapshot.agents) == ["a", "b", "c", "d"]


async def test_a_terminated_registry_reads_as_empty_and_the_next_write_starts_afresh(
    env: WorkflowEnvironment, prefix: str
) -> None:
    async with workflow_registry_store(env.client) as store:
        await store.write(RegistrySnapshot.of([_agent("a")]), 0)
        await env.client.get_workflow_handle(f"{prefix}-agent-kit-registry").terminate()

        assert await store.read() == VersionedSnapshot()
        assert await store.write(RegistrySnapshot.of([_agent("b")]), 0) == 1
        assert list((await store.read()).snapshot.agents) == ["b"]


async def test_a_newer_revision_continues_the_registry_as_new_with_its_document(
    env: WorkflowEnvironment, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_id = f"{prefix}-agent-kit-registry"
    async with workflow_registry_store(env.client) as store:
        await store.write(RegistrySnapshot.of([_agent("a")]), 0)
    first_run = (await env.client.get_workflow_handle(registry_id).describe()).run_id

    monkeypatch.setattr(registry_workflow, "REGISTRY_WORKFLOW_REVISION", 2)
    async with workflow_registry_store(env.client) as store:
        async with asyncio.timeout(10):
            while (await env.client.get_workflow_handle(registry_id).describe()).run_id == first_run:
                await asyncio.sleep(0.05)
        stored = await store.read()
        assert stored.version == 1
        assert list(stored.snapshot.agents) == ["a"]

        # The same revision starting again moves nothing.
        second_run = (await env.client.get_workflow_handle(registry_id).describe()).run_id
        await store.ensure_revision()
        assert (await env.client.get_workflow_handle(registry_id).describe()).run_id == second_run


async def test_an_older_revision_does_not_move_the_registry_back(
    env: WorkflowEnvironment, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_id = f"{prefix}-agent-kit-registry"
    monkeypatch.setattr(registry_workflow, "REGISTRY_WORKFLOW_REVISION", 3)
    async with workflow_registry_store(env.client) as store:
        await store.write(RegistrySnapshot.of([_agent("a")]), 0)
    run = (await env.client.get_workflow_handle(registry_id).describe()).run_id

    monkeypatch.setattr(registry_workflow, "REGISTRY_WORKFLOW_REVISION", 2)
    async with workflow_registry_store(env.client) as store:
        assert list((await store.read()).snapshot.agents) == ["a"]

    assert (await env.client.get_workflow_handle(registry_id).describe()).run_id == run


async def test_the_recorded_revision_1_history_replays() -> None:
    history = WorkflowHistory.from_json(
        "agent-kit-registry", REVISION_1_HISTORY.read_text()
    )
    await Replayer(
        workflows=[AgentRegistryWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)


async def test_record_the_current_revisions_history(
    env: WorkflowEnvironment, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Records a history of the current revision for a later revision's replay test, into the
    test's undeclared outputs. Runs only with ``--test_env=AGENT_KIT_RECORD_REGISTRY_HISTORY=1``."""
    if not os.environ.get("AGENT_KIT_RECORD_REGISTRY_HISTORY"):
        pytest.skip("Set AGENT_KIT_RECORD_REGISTRY_HISTORY=1 to record the history.")
    revision = registry_workflow.REGISTRY_WORKFLOW_REVISION
    registry_id = f"{prefix}-agent-kit-registry"
    async with workflow_registry_store(env.client) as store:
        await store.write(RegistrySnapshot.of([_agent("a")]), 0)
        await store.write(RegistrySnapshot.of([_agent("a"), _agent("b")]), 1)
        first_run = (await env.client.get_workflow_handle(registry_id).describe()).run_id
        monkeypatch.setattr(registry_workflow, "REGISTRY_WORKFLOW_REVISION", revision + 1)
        await store.ensure_revision()
        handle = env.client.get_workflow_handle(registry_id, run_id=first_run)
        async with asyncio.timeout(10):
            while (await handle.describe()).status != WorkflowExecutionStatus.CONTINUED_AS_NEW:
                await asyncio.sleep(0.05)
    outputs = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    history = (await handle.fetch_history()).to_json()
    (outputs / f"registry_workflow_revision_{revision}.history.json").write_text(history)
