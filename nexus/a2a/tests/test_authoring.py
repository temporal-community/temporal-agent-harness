from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import GetTaskRequest, Task
from nexus_a2a import (
    A2ABackendError,
    BackendErrorKind,
    NexusA2AServiceHandler,
)
from nexusrpc import HandlerError


async def test_generic_handler_delegates_to_backend_with_request_identity() -> None:
    backend = MagicMock()
    backend.get_task = AsyncMock(return_value=Task(id="task-1"))
    handler = NexusA2AServiceHandler(backend)
    context = MagicMock(
        request_id="request-1",
        service="A2AService",
        operation="GetTask",
        headers={"trace-id": "trace-1"},
        request_deadline=None,
    )
    request = GetTaskRequest(id="task-1")

    result = await handler.get_task(context, request)

    assert result.id == "task-1"
    operation_context, forwarded = backend.get_task.await_args.args
    assert operation_context.request_id == "request-1"
    assert operation_context.headers == {"trace-id": "trace-1"}
    assert forwarded is request


async def test_generic_handler_maps_typed_backend_errors() -> None:
    backend = MagicMock()
    backend.get_task = AsyncMock(
        side_effect=A2ABackendError("unknown task", kind=BackendErrorKind.NOT_FOUND)
    )
    handler = NexusA2AServiceHandler(backend)

    with pytest.raises(HandlerError, match="unknown task"):
        await handler.get_task(
            MagicMock(
                request_id="request-1",
                service="A2AService",
                operation="GetTask",
                headers={},
                request_deadline=None,
            ),
            GetTaskRequest(id="x"),
        )
