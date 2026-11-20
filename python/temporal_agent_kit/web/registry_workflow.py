"""The registry store the server uses by default: the document held by one Temporal workflow.

:class:`AgentRegistryWorkflow` holds a :class:`VersionedSnapshot` and knows nothing about agents.
It has one Update, ``write``, whose validator refuses a stale version, and one query, ``read``.
Writes are Update-with-Start, so the first write creates the workflow; reads are a plain query, and
a registry that does not exist or is not running reads as empty.

:func:`workflow_registry_store` runs a worker for the workflow inside the server, so every server
process sharing a registry can serve it.

The workflow runs for as long as the deployment does, so a server release must replay history an
earlier release wrote. It sends no commands besides continue-as-new, and it continues as new when a
server with a newer :data:`REGISTRY_WORKFLOW_REVISION` starts, so a release only replays history its
own revision or the one before it wrote. Bump the revision with any change to this workflow's code
or to the models it holds, and keep both readable by the revision before.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowQueryRejectedError,
    WorkflowUpdateFailedError,
)
from temporalio.common import QueryRejectCondition, WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from temporal_agent_kit.utils.task_queue import task_queue_name

with workflow.unsafe.imports_passed_through():
    from temporal_agent_kit.web.registry_store import (
        RegistrySnapshot,
        StaleRegistry,
        VersionedSnapshot,
    )

REGISTRY_WORKFLOW_REVISION = 1
"""The revision of :class:`AgentRegistryWorkflow`'s code and models."""

REGISTRY_NAME = "agent-kit-registry"
"""The registry workflow's id and task queue, each under ``TASK_QUEUE_PREFIX``."""

_STALE_REGISTRY = "StaleRegistry"
_NOT_NEWER = "RevisionNotNewer"


class RegistryRun(BaseModel):
    """What one run of the registry workflow starts from."""

    registry: VersionedSnapshot
    # The REGISTRY_WORKFLOW_REVISION of the server that started this run.
    revision: int


class RegistryWrite(BaseModel):
    snapshot: RegistrySnapshot
    expected: int


@workflow.defn(name="AgentKitRegistry")
class AgentRegistryWorkflow:
    """Holds the agent registry's document. See the module notes."""

    @workflow.init
    def __init__(self, run: RegistryRun) -> None:
        self._registry = run.registry
        self._revision = run.revision

    @workflow.run
    async def run(self, run: RegistryRun) -> None:
        await workflow.wait_condition(
            lambda: (
                self._revision > run.revision or workflow.info().is_continue_as_new_suggested()
            )
            and workflow.all_handlers_finished()
        )
        workflow.continue_as_new(RegistryRun(registry=self._registry, revision=self._revision))

    @workflow.query
    def read(self) -> VersionedSnapshot:
        return self._registry

    @workflow.update
    def write(self, write: RegistryWrite) -> int:
        self._registry = VersionedSnapshot(snapshot=write.snapshot, version=write.expected + 1)
        return self._registry.version

    @write.validator
    def _validate_write(self, write: RegistryWrite) -> None:
        if write.expected != self._registry.version:
            raise ApplicationError(
                f"The registry is at version {self._registry.version}, not {write.expected}.",
                type=_STALE_REGISTRY,
                non_retryable=True,
            )

    @workflow.update
    def ensure_revision(self, revision: int) -> None:
        """Continue as new under the newer ``revision`` once the handlers running now finish."""
        self._revision = revision

    @ensure_revision.validator
    def _validate_ensure_revision(self, revision: int) -> None:
        if revision <= self._revision:
            raise ApplicationError(
                f"This run is already at revision {self._revision}.",
                type=_NOT_NEWER,
                non_retryable=True,
            )


class WorkflowRegistryStore:
    """An :class:`~temporal_agent_kit.web.registry_store.AgentRegistryStore` over
    :class:`AgentRegistryWorkflow`."""

    def __init__(self, client: Client, *, workflow_id: str, task_queue: str) -> None:
        self._client = client
        self._workflow_id = workflow_id
        self._task_queue = task_queue

    async def read(self) -> VersionedSnapshot:
        handle = self._client.get_workflow_handle(self._workflow_id)
        try:
            return await handle.query(
                AgentRegistryWorkflow.read, reject_condition=QueryRejectCondition.NOT_OPEN
            )
        except WorkflowQueryRejectedError:
            return VersionedSnapshot()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return VersionedSnapshot()
            raise

    async def write(self, snapshot: RegistrySnapshot, expected: int) -> int:
        start = WithStartWorkflowOperation(
            AgentRegistryWorkflow.run,
            RegistryRun(registry=VersionedSnapshot(), revision=REGISTRY_WORKFLOW_REVISION),
            id=self._workflow_id,
            task_queue=self._task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        try:
            return await self._client.execute_update_with_start_workflow(
                AgentRegistryWorkflow.write,
                RegistryWrite(snapshot=snapshot, expected=expected),
                start_workflow_operation=start,
            )
        except WorkflowUpdateFailedError as e:
            if getattr(e.cause, "type", None) == _STALE_REGISTRY:
                raise StaleRegistry(str(e.cause)) from e
            raise

    async def ensure_revision(self) -> None:
        """Move a registry an older server revision started onto this one."""
        handle = self._client.get_workflow_handle(self._workflow_id)
        try:
            await handle.execute_update(
                AgentRegistryWorkflow.ensure_revision, REGISTRY_WORKFLOW_REVISION
            )
        except WorkflowUpdateFailedError as e:
            if getattr(e.cause, "type", None) != _NOT_NEWER:
                raise
        except RPCError as e:
            # No registry yet, or none running: the next write starts one at this revision.
            if e.status not in (RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION):
                raise


@asynccontextmanager
async def workflow_registry_store(client: Client) -> AsyncIterator[WorkflowRegistryStore]:
    """The default :data:`~temporal_agent_kit.web.registry_store.RegistryStoreFactory`: the
    registry workflow under this process's ``TASK_QUEUE_PREFIX``, with a worker for it running
    while the store is entered."""
    name = task_queue_name(REGISTRY_NAME)
    # A client without the server client's plugins, so the worker hosts only the registry
    # workflow (a worker takes its client's plugins, and AgentKitPlugin adds agent activities).
    # The data converter is the configured one, so both read the same payloads.
    worker_client = Client(
        client.service_client,
        namespace=client.namespace,
        data_converter=client.data_converter,
    )
    async with Worker(worker_client, task_queue=name, workflows=[AgentRegistryWorkflow]):
        store = WorkflowRegistryStore(client, workflow_id=name, task_queue=name)
        await store.ensure_revision()
        yield store
