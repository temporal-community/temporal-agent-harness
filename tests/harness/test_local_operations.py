"""Unit tests for the workflow-independent local-operation protocol."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness.local_operations import (
    COMPLETE_LOCAL_OPERATION_UPDATE,
    PENDING_LOCAL_OPERATIONS_QUERY,
    DuplicateLocalOperation,
    InvalidLocalOperationRequest,
    LocalOperationAck,
    LocalOperationAlreadyResolved,
    LocalOperationFailed,
    LocalOperationRequest,
    LocalOperationResult,
    LocalOperationStatus,
    LocalOperations,
    MalformedLocalOperationResult,
    UnknownLocalOperation,
)


class Transcript(BaseModel):
    text: str
    speakers: int


@workflow.defn
class LocalOperationsProbeWorkflow:
    """Small real workflow proving the reusable query/update/wait wiring."""

    @workflow.init
    def __init__(self, operation: LocalOperationRequest) -> None:
        self._operation = operation
        self._operations = LocalOperations()

    @workflow.run
    async def run(self, operation: LocalOperationRequest) -> Transcript:
        assert operation == self._operation
        return await self._operations.run(operation, Transcript)

    @workflow.query(name=PENDING_LOCAL_OPERATIONS_QUERY)
    def pending_local_operations(self) -> list[LocalOperationRequest]:
        return self._operations.pending()

    @workflow.update(name=COMPLETE_LOCAL_OPERATION_UPDATE)
    async def complete_local_operation(
        self, result: LocalOperationResult
    ) -> LocalOperationAck:
        return self._operations.complete(result)

    @complete_local_operation.validator
    def validate_complete_local_operation(self, result: LocalOperationResult) -> None:
        self._operations.validate_completion(result)


def request(
    operation_id: str = "job/7",
    *,
    idempotency_key: str | None = None,
) -> LocalOperationRequest:
    return LocalOperationRequest(
        operation_id=operation_id,
        bridge_id="device-abc",
        root_id="campaign-main",
        kind="read_transcript",
        arguments={"session_id": "s1"},
        idempotency_key=idempotency_key or operation_id,
    )


async def cooperative_wait(condition, *, timeout=None) -> None:
    del timeout
    while not condition():
        await asyncio.sleep(0)


def test_start_exposes_typed_pending_request_and_status() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)

    first = operations.start(request("job/1"), Transcript)
    second = operations.start(request("job/2"), str)

    assert first.bridge_id == "device-abc"
    assert first.root_id == "campaign-main"
    assert first.output_schema["properties"]["text"]["type"] == "string"
    assert [item.operation_id for item in operations.pending()] == ["job/1", "job/2"]
    assert operations.status("job/1").status is LocalOperationStatus.PENDING
    assert operations.is_resolved("job/1") is False
    assert second.output_schema["type"] == "string"


def test_start_is_idempotent_but_rejects_conflicting_ids_and_keys() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)
    original = request()

    assert operations.start(original, Transcript) == operations.start(original, Transcript)

    changed = LocalOperationRequest(
        **{
            **original.__dict__,
            "kind": "write_transcript",
            "output_schema": {},
        }
    )
    with pytest.raises(DuplicateLocalOperation, match="different request"):
        operations.start(changed, Transcript)

    with pytest.raises(DuplicateLocalOperation, match="idempotency_key"):
        operations.start(request("job/8", idempotency_key="job/7"), Transcript)


@pytest.mark.parametrize(
    "field",
    ["operation_id", "bridge_id", "root_id", "kind", "idempotency_key"],
)
def test_start_rejects_blank_routing_and_identity_fields(field: str) -> None:
    values = {**request().__dict__, field: "  "}
    operations = LocalOperations(wait_condition=cooperative_wait)

    with pytest.raises(InvalidLocalOperationRequest, match=field):
        operations.start(LocalOperationRequest(**values), Transcript)


def test_malformed_completion_does_not_consume_pending_gate() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)
    operations.start(request(), Transcript)
    malformed = LocalOperationResult(
        operation_id="job/7", result={"text": "hello"}
    )

    with pytest.raises(MalformedLocalOperationResult):
        operations.validate_completion(malformed)
    with pytest.raises(MalformedLocalOperationResult):
        operations.complete(malformed)

    assert [item.operation_id for item in operations.pending()] == ["job/7"]
    assert operations.status("job/7").status is LocalOperationStatus.PENDING


@pytest.mark.asyncio
async def test_run_waits_for_completion_and_returns_coerced_type() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)
    task = asyncio.create_task(operations.run(request(), Transcript))
    await asyncio.sleep(0)

    operations.validate_completion(
        LocalOperationResult(
            operation_id="job/7", result={"text": "hello", "speakers": "3"}
        )
    )
    ack = operations.complete(
        LocalOperationResult(
            operation_id="job/7", result={"text": "hello", "speakers": "3"}
        )
    )

    assert ack.operation_id == "job/7"
    assert ack.accepted is True
    assert await task == Transcript(text="hello", speakers=3)
    assert operations.pending() == []
    assert operations.status("job/7").status is LocalOperationStatus.SUCCEEDED
    assert operations.is_resolved("job/7") is True

    with pytest.raises(LocalOperationAlreadyResolved, match="succeeded"):
        operations.complete(LocalOperationResult(operation_id="job/7", result={}))


@pytest.mark.asyncio
async def test_bridge_error_is_terminal_and_raises_from_wait() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)
    operations.start(request(), Transcript)

    operations.complete(
        LocalOperationResult(operation_id="job/7", error="permission denied")
    )

    with pytest.raises(LocalOperationFailed, match="permission denied") as failure:
        await operations.await_result("job/7")
    assert failure.value.outcome.status is LocalOperationStatus.FAILED
    assert operations.status("job/7").result is None


@pytest.mark.asyncio
async def test_none_is_a_valid_success_result() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)
    operations.start(request(), type(None))

    operations.complete(LocalOperationResult(operation_id="job/7", result=None))

    assert await operations.await_result("job/7") is None
    assert operations.status("job/7").status is LocalOperationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_timeout_marks_operation_and_late_completion_is_rejected() -> None:
    async def timeout_wait(condition, *, timeout=None) -> None:
        assert condition() is False
        assert timeout == timedelta(seconds=5)
        raise TimeoutError

    operations = LocalOperations(wait_condition=timeout_wait)
    operations.start(request(), str)

    with pytest.raises(LocalOperationFailed, match="timed_out") as failure:
        await operations.await_result("job/7", timeout=timedelta(seconds=5))

    assert failure.value.outcome.status is LocalOperationStatus.TIMED_OUT
    assert operations.pending() == []
    with pytest.raises(LocalOperationAlreadyResolved, match="timed_out"):
        operations.complete(LocalOperationResult(operation_id="job/7", result="late"))


@pytest.mark.asyncio
async def test_cancel_pending_wakes_waiters_in_registration_order() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)
    operations.start(request("job/1"), str)
    operations.start(request("job/2"), str)
    waiter = asyncio.create_task(operations.await_result("job/2"))
    await asyncio.sleep(0)

    assert operations.cancel_pending(reason="workflow closed") == ["job/1", "job/2"]
    with pytest.raises(LocalOperationFailed, match="workflow closed") as failure:
        await waiter
    assert failure.value.outcome.status is LocalOperationStatus.CANCELLED


def test_unknown_operation_is_rejected_consistently() -> None:
    operations = LocalOperations(wait_condition=cooperative_wait)

    with pytest.raises(UnknownLocalOperation):
        operations.status("missing")
    with pytest.raises(UnknownLocalOperation):
        operations.validate_completion(LocalOperationResult(operation_id="missing"))


@pytest.mark.asyncio
async def test_temporal_query_validated_update_and_wait_integration() -> None:
    """Exercise LocalOperations inside a real time-skipping Temporal workflow."""
    environment = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"local-operations-{uuid.uuid4()}"
    try:
        async with Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[LocalOperationsProbeWorkflow],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            operation = request("job/temporal")
            handle = await environment.client.start_workflow(
                LocalOperationsProbeWorkflow.run,
                operation,
                id=f"local-operations-probe-{uuid.uuid4()}",
                task_queue=task_queue,
                result_type=Transcript,
            )

            pending = await handle.query(
                PENDING_LOCAL_OPERATIONS_QUERY,
                result_type=list[LocalOperationRequest],
            )
            assert [item.operation_id for item in pending] == ["job/temporal"]
            assert pending[0].output_schema["properties"]["speakers"]["type"] == "integer"

            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update(
                    COMPLETE_LOCAL_OPERATION_UPDATE,
                    LocalOperationResult(
                        operation_id="job/temporal",
                        result={"text": "hello"},
                    ),
                    result_type=LocalOperationAck,
                )

            # Validator rejection is mutation-free: the same waiter remains pending and accepts
            # a corrected typed completion.
            pending_after_rejection = await handle.query(
                PENDING_LOCAL_OPERATIONS_QUERY,
                result_type=list[LocalOperationRequest],
            )
            assert [item.operation_id for item in pending_after_rejection] == [
                "job/temporal"
            ]

            ack = await handle.execute_update(
                COMPLETE_LOCAL_OPERATION_UPDATE,
                LocalOperationResult(
                    operation_id="job/temporal",
                    result={"text": "hello", "speakers": "3"},
                ),
                result_type=LocalOperationAck,
            )
            assert ack == LocalOperationAck(operation_id="job/temporal")
            assert await handle.result() == Transcript(text="hello", speakers=3)
    finally:
        await environment.shutdown()
