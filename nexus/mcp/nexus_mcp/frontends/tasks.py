"""MCP Tasks extension backed by standalone Nexus operations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp import types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import (
    Context,
    Extension,
    MethodBinding,
    require_client_extension,
)
from mcp.shared.exceptions import MCPError

from nexus_mcp.resolver import (
    NexusTask,
    NexusToolResolver,
    RequestContext,
    UnknownToolError,
)
from nexus_mcp.tasks import (
    MODERN_PROTOCOL_VERSION,
    TASKS_EXTENSION,
    CreateTaskResult,
    DetailedTaskResult,
    EmptyTaskResult,
    GetTaskParams,
    UpdateTaskParams,
)

from .bridge import request_context_from_mcp


def _timestamp(value: Any) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _task_result(
    task: NexusTask,
    *,
    ttl_ms: int | None,
    poll_interval_ms: int,
    detailed: bool,
) -> CreateTaskResult | DetailedTaskResult:
    values: dict[str, Any] = {
        "task_id": task.task_id,
        "status": task.status,
        "created_at": _timestamp(task.created_at),
        "last_updated_at": _timestamp(task.last_updated_at),
        "ttl_ms": ttl_ms,
        "poll_interval_ms": poll_interval_ms,
    }
    if not detailed:
        return CreateTaskResult(**values)
    if task.status == "completed":
        values["result"] = task.result.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
    elif task.status == "failed":
        message = task.error or "The Nexus operation failed."
        values["status_message"] = message
        values["error"] = types.ErrorData(
            code=types.INTERNAL_ERROR,
            message=message,
        )
    return DetailedTaskResult(**values)


def _supports_tasks(ctx: ServerRequestContext[Any, Any]) -> bool:
    if ctx.protocol_version != MODERN_PROTOCOL_VERSION:
        return False
    capabilities = ctx.session.client_capabilities
    return bool(
        capabilities is not None
        and capabilities.extensions is not None
        and TASKS_EXTENSION in capabilities.extensions
    )


class NexusTasksExtension(Extension):
    """Expose the 2026 Tasks extension for durable Nexus operations."""

    identifier = TASKS_EXTENSION

    def __init__(
        self,
        resolver: NexusToolResolver,
        *,
        ttl_ms: int | None,
        poll_interval_ms: int,
    ) -> None:
        self._resolver = resolver
        self._ttl_ms = ttl_ms
        self._poll_interval_ms = poll_interval_ms

    def methods(self) -> Sequence[MethodBinding]:
        versions = frozenset({MODERN_PROTOCOL_VERSION})
        return (
            MethodBinding(
                method="tasks/get",
                params_type=GetTaskParams,
                handler=self._get_task,
                protocol_versions=versions,
            ),
            MethodBinding(
                method="tasks/update",
                params_type=UpdateTaskParams,
                handler=self._update_task,
                protocol_versions=versions,
            ),
            MethodBinding(
                method="tasks/cancel",
                params_type=GetTaskParams,
                handler=self._cancel_task,
                protocol_versions=versions,
            ),
        )

    async def intercept_tool_call(
        self,
        params: types.CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if not _supports_tasks(ctx):
            return await call_next(ctx)
        try:
            task = await self._resolver.start_tool_task(
                params.name,
                params.arguments,
                context=request_context_from_mcp_context(ctx),
            )
        except UnknownToolError as exc:
            raise MCPError(code=types.INVALID_PARAMS, message=str(exc)) from exc
        if task.status == "completed":
            return task.result
        return _task_result(
            task,
            ttl_ms=self._ttl_ms,
            poll_interval_ms=self._poll_interval_ms,
            detailed=False,
        )

    async def _get_task(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: GetTaskParams,
    ) -> DetailedTaskResult:
        require_client_extension(ctx, self.identifier)
        try:
            task = await self._resolver.get_task(params.task_id)
        except ValueError as exc:
            raise MCPError(code=types.INVALID_PARAMS, message=str(exc)) from exc
        result = _task_result(
            task,
            ttl_ms=self._ttl_ms,
            poll_interval_ms=self._poll_interval_ms,
            detailed=True,
        )
        assert isinstance(result, DetailedTaskResult)
        return result

    async def _update_task(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: UpdateTaskParams,
    ) -> EmptyTaskResult:
        require_client_extension(ctx, self.identifier)
        try:
            await self._resolver.get_task(params.task_id)
        except ValueError as exc:
            raise MCPError(code=types.INVALID_PARAMS, message=str(exc)) from exc
        # Nexus operations do not request input. Ignore unmatched responses.
        return EmptyTaskResult()

    async def _cancel_task(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: GetTaskParams,
    ) -> EmptyTaskResult:
        require_client_extension(ctx, self.identifier)
        try:
            await self._resolver.cancel_task(params.task_id)
        except ValueError as exc:
            raise MCPError(code=types.INVALID_PARAMS, message=str(exc)) from exc
        return EmptyTaskResult()


def request_context_from_mcp_context(
    context: ServerRequestContext[Any, Any],
) -> RequestContext:
    """Convert a low-level SDK context for an extension call."""
    return request_context_from_mcp(Context(request_context=context))
