"""Generic Nexus service handler for an A2A task backend."""

from __future__ import annotations

from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetExtendedAgentCardRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    SendMessageResponse,
    Task,
)
from nexusrpc import HandlerError, HandlerErrorType
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from temporalio import nexus

from nexus_a2a.errors import A2ABackendError, BackendErrorKind
from nexus_a2a.service import A2AService, SubscribeToTaskInput, SubscribeToTaskOutput

from .backend import A2ABackend, OperationContext

_ERROR_TYPES = {
    BackendErrorKind.BAD_REQUEST: HandlerErrorType.BAD_REQUEST,
    BackendErrorKind.NOT_FOUND: HandlerErrorType.NOT_FOUND,
    BackendErrorKind.CONFLICT: HandlerErrorType.CONFLICT,
    BackendErrorKind.INTERNAL: HandlerErrorType.INTERNAL,
}


def _context(ctx: StartOperationContext | nexus.TemporalStartOperationContext):
    return OperationContext(
        request_id=str(ctx.request_id),
        service=ctx.service,
        operation=ctx.operation,
        headers=ctx.headers,
        request_deadline=ctx.request_deadline,
    )


async def _call(awaitable):
    try:
        return await awaitable
    except A2ABackendError as exc:
        raise HandlerError(str(exc), type=_ERROR_TYPES[exc.kind]) from exc


@service_handler(service=A2AService)
class NexusA2AServiceHandler:
    """Expose any A2A backend through the Temporal Nexus binding."""

    def __init__(self, backend: A2ABackend) -> None:
        self._backend = backend

    @sync_operation
    async def send_message(
        self, ctx: StartOperationContext, request: SendMessageRequest
    ) -> SendMessageResponse:
        return await _call(self._backend.send_message(_context(ctx), request))

    @sync_operation
    async def get_task(
        self, ctx: StartOperationContext, request: GetTaskRequest
    ) -> Task:
        return await _call(self._backend.get_task(_context(ctx), request))

    @sync_operation
    async def list_tasks(
        self, ctx: StartOperationContext, request: ListTasksRequest
    ) -> ListTasksResponse:
        return await _call(self._backend.list_tasks(_context(ctx), request))

    @sync_operation
    async def cancel_task(
        self, ctx: StartOperationContext, request: CancelTaskRequest
    ) -> Task:
        return await _call(self._backend.cancel_task(_context(ctx), request))

    @sync_operation
    async def get_extended_agent_card(
        self, ctx: StartOperationContext, request: GetExtendedAgentCardRequest
    ) -> AgentCard:
        return await _call(
            self._backend.get_extended_agent_card(_context(ctx), request)
        )

    @nexus.temporal_operation
    async def subscribe_to_task(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        request: SubscribeToTaskInput,
    ) -> nexus.TemporalOperationResult[SubscribeToTaskOutput]:
        return await _call(
            self._backend.subscribe_to_task(_context(ctx), client, request)
        )
