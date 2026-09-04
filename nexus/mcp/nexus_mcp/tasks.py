"""MCP Tasks extension types and client helpers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import anyio
from mcp import types
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from pydantic import model_serializer

MODERN_PROTOCOL_VERSION = "2026-07-28"
TASKS_EXTENSION = "io.modelcontextprotocol/tasks"


class CreateTaskResult(types.Result):
    """Return a task handle from a task-capable tool call."""

    result_type: Literal["task"] = "task"
    task_id: str
    status: Literal["working", "completed", "failed", "cancelled"]
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        result = handler(self)
        result["ttlMs"] = self.ttl_ms
        return result


class GetTaskParams(types.RequestParams):
    """Select one task."""

    task_id: str


class UpdateTaskParams(GetTaskParams):
    """Provide responses to a task that requested input."""

    input_responses: dict[str, Any]


class DetailedTaskResult(types.Result):
    """Return the current task state and terminal result."""

    result_type: Literal["complete"] = "complete"
    task_id: str
    status: Literal["working", "completed", "failed", "cancelled"]
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None
    poll_interval_ms: int | None = None
    result: dict[str, Any] | None = None
    error: types.ErrorData | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        result = handler(self)
        result["ttlMs"] = self.ttl_ms
        return result


class EmptyTaskResult(types.Result):
    """Acknowledge a task update or cancellation."""

    result_type: Literal["complete"] = "complete"


async def get_task(session: ClientSession, task_id: str) -> DetailedTaskResult:
    """Get the current state of one MCP task."""
    request = types.Request(
        method="tasks/get",
        params=GetTaskParams(task_id=task_id),
    )
    return await session.send_request(request, DetailedTaskResult)


async def cancel_task(session: ClientSession, task_id: str) -> EmptyTaskResult:
    """Request cancellation of one MCP task."""
    request = types.Request(
        method="tasks/cancel",
        params=GetTaskParams(task_id=task_id),
    )
    return await session.send_request(request, EmptyTaskResult)


async def update_task(
    session: ClientSession,
    task_id: str,
    input_responses: dict[str, Any],
) -> EmptyTaskResult:
    """Send requested input to one MCP task."""
    request = types.Request(
        method="tasks/update",
        params=UpdateTaskParams(
            task_id=task_id,
            input_responses=input_responses,
        ),
    )
    return await session.send_request(request, EmptyTaskResult)


class NexusTasksClientExtension(ClientExtension):
    """Advertise Tasks support and resolve task results by polling."""

    identifier = TASKS_EXTENSION

    def claims(self) -> Sequence[ResultClaim[CreateTaskResult]]:
        return (
            ResultClaim(
                result_type="task",
                model=CreateTaskResult,
                resolve=self._resolve,
                protocol_versions=frozenset({MODERN_PROTOCOL_VERSION}),
            ),
        )

    async def _resolve(
        self,
        task: CreateTaskResult,
        context: ClaimContext,
    ) -> types.CallToolResult:
        while True:
            current = await get_task(context.session, task.task_id)
            if current.status == "completed" and current.result is not None:
                return types.CallToolResult.model_validate(current.result)
            if current.status in {"failed", "cancelled"}:
                error = current.error or types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=current.status_message or f"Task {current.status}.",
                )
                raise MCPError.from_error_data(error)
            delay_ms = current.poll_interval_ms or task.poll_interval_ms or 1_000
            await anyio.sleep(delay_ms / 1_000)


__all__ = [
    "MODERN_PROTOCOL_VERSION",
    "TASKS_EXTENSION",
    "CreateTaskResult",
    "DetailedTaskResult",
    "EmptyTaskResult",
    "GetTaskParams",
    "NexusTasksClientExtension",
    "UpdateTaskParams",
    "cancel_task",
    "get_task",
    "update_task",
]
