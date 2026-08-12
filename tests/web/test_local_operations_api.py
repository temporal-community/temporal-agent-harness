from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.local_operations import (
    COMPLETE_LOCAL_OPERATION_UPDATE,
    PENDING_LOCAL_OPERATIONS_QUERY,
    LocalOperationAck,
    LocalOperationRequest,
    LocalOperationResult,
)
from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app


def _operation(
    operation_id: str,
    *,
    bridge_id: str = "browser-1",
    root_id: str = "campaign",
    kind: str = "read_text",
) -> LocalOperationRequest:
    return LocalOperationRequest(
        operation_id=operation_id,
        bridge_id=bridge_id,
        root_id=root_id,
        kind=kind,
        arguments={"path": "notes.md"},
        idempotency_key=f"key-{operation_id}",
        output_schema={"type": "string"},
    )


class _LocalOperationsHandle:
    def __init__(self, pending: list[LocalOperationRequest]) -> None:
        self.pending = pending
        self.query_error: BaseException | None = None
        self.update_error: BaseException | None = None
        self.query_calls: list[tuple[Any, dict[str, Any]]] = []
        self.update_calls: list[tuple[Any, Any, dict[str, Any]]] = []

    async def query(self, query: Any, **kwargs: Any) -> list[LocalOperationRequest]:
        self.query_calls.append((query, kwargs))
        if self.query_error is not None:
            raise self.query_error
        return self.pending

    async def execute_update(
        self,
        update: Any,
        arg: LocalOperationResult,
        **kwargs: Any,
    ) -> LocalOperationAck:
        self.update_calls.append((update, arg, kwargs))
        if self.update_error is not None:
            raise self.update_error
        return LocalOperationAck(operation_id=arg.operation_id)


class _TemporalClient:
    def __init__(self, handle: _LocalOperationsHandle) -> None:
        self.handle = handle
        self.workflow_ids: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _LocalOperationsHandle:
        self.workflow_ids.append(workflow_id)
        return self.handle


def _client(
    pending: list[LocalOperationRequest],
) -> tuple[TestClient, _TemporalClient, _LocalOperationsHandle]:
    handle = _LocalOperationsHandle(pending)
    temporal = _TemporalClient(handle)
    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.temporal = temporal
    return TestClient(app), temporal, handle


def test_pending_operations_returns_matching_durable_snapshot_in_workflow_order() -> None:
    first = _operation("job-1", kind="read_text")
    second = _operation("job-2", kind="write_text")
    client, temporal, handle = _client(
        [
            _operation("other-bridge", bridge_id="browser-2"),
            first,
            second,
            _operation("other-root", root_id="other"),
        ]
    )

    response = client.get(
        "/api/local-operations",
        params=[
            ("workflow_id", "pipeline-1"),
            ("bridge_id", "browser-1"),
            ("root_id", "campaign"),
            ("capability", "read_text"),
            ("capability", "write_text"),
        ],
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "workflow_id": "pipeline-1",
        "bridge_id": "browser-1",
        "root_id": "campaign",
        "operations": [
            {
                "operation_id": "job-1",
                "bridge_id": "browser-1",
                "root_id": "campaign",
                "kind": "read_text",
                "arguments": {"path": "notes.md"},
                "idempotency_key": "key-job-1",
                "output_schema": {"type": "string"},
            },
            {
                "operation_id": "job-2",
                "bridge_id": "browser-1",
                "root_id": "campaign",
                "kind": "write_text",
                "arguments": {"path": "notes.md"},
                "idempotency_key": "key-job-2",
                "output_schema": {"type": "string"},
            },
        ],
    }
    assert temporal.workflow_ids == ["pipeline-1"]
    assert handle.query_calls[0][0] == PENDING_LOCAL_OPERATIONS_QUERY
    assert handle.update_calls == []


@pytest.mark.parametrize("missing", ["bridge_id", "root_id"])
def test_pending_operations_requires_explicit_routing_fields(missing: str) -> None:
    client, temporal, _handle = _client([])
    params = {"bridge_id": "browser-1", "root_id": "campaign"}
    del params[missing]

    response = client.get(
        "/api/local-operations",
        params={"workflow_id": "pipeline-1", **params},
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []


def test_canonical_completion_supports_slashes_in_workflow_and_operation_ids() -> None:
    operation = _operation("chapter/7")
    client, temporal, handle = _client([operation])

    response = client.post(
        "/api/local-operation-results",
        json={
            "workflow_id": "campaigns/acme/plain-workflow",
            "operation_id": "chapter/7",
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "success",
            "result": "chapter text",
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"operation_id": "chapter/7", "accepted": True}
    assert temporal.workflow_ids == [
        "campaigns/acme/plain-workflow",
        "campaigns/acme/plain-workflow",
    ]
    update, result, kwargs = handle.update_calls[0]
    assert update == COMPLETE_LOCAL_OPERATION_UPDATE
    assert result == LocalOperationResult(
        operation_id="chapter/7",
        result="chapter text",
    )
    assert kwargs["result_type"] is LocalOperationAck


def test_canonical_poll_treats_slash_containing_workflow_id_as_opaque() -> None:
    client, temporal, _handle = _client([_operation("job-1")])

    response = client.get(
        "/api/local-operations",
        params={
            "workflow_id": "campaigns/acme/pipeline-1",
            "bridge_id": "browser-1",
            "root_id": "campaign",
        },
    )

    assert response.status_code == 200
    assert response.json()["workflow_id"] == "campaigns/acme/pipeline-1"
    assert temporal.workflow_ids == ["campaigns/acme/pipeline-1"]


def test_error_completion_forwards_failure_without_a_result() -> None:
    client, _temporal, handle = _client([_operation("job-1")])

    response = client.post(
        "/api/local-operation-results",
        json={
            "workflow_id": "pipeline-1",
            "operation_id": "job-1",
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "error",
            "error": "permission was revoked",
        },
    )

    assert response.status_code == 200
    assert handle.update_calls[0][1] == LocalOperationResult(
        operation_id="job-1",
        error="permission was revoked",
    )


def test_completion_rejects_wrong_bridge_or_root_before_workflow_update() -> None:
    client, _temporal, handle = _client([_operation("job-1")])

    response = client.post(
        "/api/local-operation-results",
        json={
            "workflow_id": "pipeline-1",
            "operation_id": "job-1",
            "bridge_id": "browser-2",
            "root_id": "campaign",
            "outcome": "success",
            "result": "text",
        },
    )

    assert response.status_code == 409
    assert handle.update_calls == []


def test_completion_reports_non_pending_operation_as_authoritatively_settled() -> None:
    client, _temporal, handle = _client([])

    response = client.post(
        "/api/local-operation-results",
        json={
            "workflow_id": "pipeline-1",
            "operation_id": "job-1",
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "success",
            "result": "text",
        },
    )

    assert response.status_code == 410
    assert response.json()["detail"] == "Local operation 'job-1' is not pending."
    assert handle.update_calls == []


def test_completion_keeps_missing_workflow_distinct_and_retryable() -> None:
    client, _temporal, handle = _client([])
    handle.query_error = RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    response = client.post(
        "/api/local-operation-results",
        json={
            "workflow_id": "pipeline-1",
            "operation_id": "job-1",
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "success",
            "result": "text",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow 'pipeline-1' was not found."
    assert handle.update_calls == []


@pytest.mark.parametrize(
    ("cause", "expected_status", "expected_error"),
    [
        (
            ApplicationError(
                "local operation is already succeeded",
                type="LocalOperationAlreadyResolved",
            ),
            410,
            "local_operation_settled",
        ),
        (
            ApplicationError(
                "does not match the declared output type",
                type="MalformedLocalOperationResult",
            ),
            422,
            "invalid_local_operation_result",
        ),
    ],
)
def test_completion_maps_update_validation_and_race_failures_to_stable_statuses(
    cause: BaseException,
    expected_status: int,
    expected_error: str,
) -> None:
    client, _temporal, handle = _client([_operation("job-1")])
    handle.update_error = WorkflowUpdateFailedError(cause)

    response = client.post(
        "/api/local-operation-results",
        json={
            "workflow_id": "campaigns/acme/pipeline-1",
            "operation_id": "job-1",
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "success",
            "result": "text",
        },
    )

    assert response.status_code == expected_status
    assert response.json()["detail"]["error"] == expected_error


@pytest.mark.parametrize(
    "payload",
    [
        {
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "error",
        },
        {
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "success",
            "error": "unexpected",
        },
        {
            "bridge_id": "browser-1",
            "root_id": "campaign",
            "outcome": "error",
            "result": "unexpected",
            "error": "failed",
        },
    ],
)
def test_completion_rejects_inconsistent_outcome_payloads(payload: dict[str, Any]) -> None:
    client, temporal, handle = _client([_operation("job-1")])

    response = client.post(
        "/api/local-operation-results",
        json={"workflow_id": "pipeline-1", "operation_id": "job-1", **payload},
    )

    assert response.status_code == 422
    assert temporal.workflow_ids == []
    assert handle.update_calls == []


def test_openapi_marks_bridge_routing_fields_as_non_authentication() -> None:
    client, _temporal, _handle = _client([])

    operation = client.get("/openapi.json").json()["paths"][
        "/api/local-operations"
    ]["get"]

    assert "not authentication" in operation["description"]
