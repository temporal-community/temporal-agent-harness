"""Protocol client for A2A agents exposed through Temporal Nexus."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetExtendedAgentCardRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    SendMessageResponse,
    StreamResponse,
    Task,
)
from google.protobuf.json_format import MessageToDict

from .context import RequestContext
from .errors import NexusA2AOperationError
from .execution.base import NexusA2AExecutor
from .service import A2AService, SubscribeToTaskInput, SubscribeToTaskOutput
from .stream import StreamRecord, decode_stream_record, is_terminal_response

DEFAULT_POLL_TIMEOUT_SECONDS = 25.0
OPERATION_TIMEOUT_GRACE_SECONDS = 5.0


class NexusA2AClient:
    """Shared A2A client used by workflow and standalone frontends.

    This class owns A2A semantics and bounded stream paging. An executor owns the
    Temporal calling context, so the same implementation works inside workflows,
    ordinary async processes, SDK transports, and harness adapters.
    """

    def __init__(
        self,
        executor: NexusA2AExecutor,
        endpoint: str,
        *,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> None:
        if poll_timeout_seconds <= 0:
            raise ValueError("poll_timeout_seconds must be positive")
        self._executor = executor
        self.endpoint = endpoint
        self.poll_timeout_seconds = poll_timeout_seconds

    async def _execute(
        self,
        operation: Any,
        request: Any,
        *,
        context: RequestContext | None = None,
    ) -> Any:
        request_context = context or RequestContext()
        try:
            return await self._executor.execute(
                service=A2AService,
                endpoint=self.endpoint,
                operation=operation,
                argument=request,
                context=request_context,
            )
        except Exception as exc:
            raise NexusA2AOperationError(
                f"Nexus A2A operation {operation.name!r} failed on "
                f"endpoint {self.endpoint!r}: {exc}"
            ) from exc

    async def send_message(
        self,
        request: SendMessageRequest,
        *,
        context: RequestContext | None = None,
    ) -> SendMessageResponse:
        return await self._execute(A2AService.send_message, request, context=context)

    async def get_task(
        self,
        request: GetTaskRequest,
        *,
        context: RequestContext | None = None,
    ) -> Task:
        return await self._execute(A2AService.get_task, request, context=context)

    async def list_tasks(
        self,
        request: ListTasksRequest,
        *,
        context: RequestContext | None = None,
    ) -> ListTasksResponse:
        return await self._execute(A2AService.list_tasks, request, context=context)

    async def cancel_task(
        self,
        request: CancelTaskRequest,
        *,
        context: RequestContext | None = None,
    ) -> Task:
        return await self._execute(A2AService.cancel_task, request, context=context)

    async def get_extended_agent_card(
        self,
        request: GetExtendedAgentCardRequest,
        *,
        context: RequestContext | None = None,
    ) -> AgentCard:
        return await self._execute(
            A2AService.get_extended_agent_card, request, context=context
        )

    async def subscribe_page(
        self,
        request: SubscribeToTaskInput,
        *,
        context: RequestContext | None = None,
    ) -> SubscribeToTaskOutput:
        """Read one bounded page without hiding its durable cursor."""

        operation_timeout = request.timeout_seconds + OPERATION_TIMEOUT_GRACE_SECONDS
        request_context = context or RequestContext()
        if request_context.timeout_seconds is None:
            request_context = RequestContext(
                request_id=request_context.request_id,
                idempotency_key=request_context.idempotency_key,
                nexus_headers=request_context.nexus_headers,
                timeout_seconds=operation_timeout,
            )
        return await self._execute(
            A2AService.subscribe_to_task, request, context=request_context
        )

    async def stream_task(
        self,
        *,
        task_id: str,
        tenant: str = "",
        cursor: int = 0,
        context: RequestContext | None = None,
    ) -> AsyncGenerator[StreamRecord]:
        """Turn bounded Nexus pages into one asynchronous A2A task stream."""

        while True:
            poll_timeout = self.poll_timeout_seconds
            if context is not None and context.timeout_seconds is not None:
                poll_timeout = min(poll_timeout, context.timeout_seconds)
            page = await self.subscribe_page(
                SubscribeToTaskInput(
                    tenant=tenant,
                    id=task_id,
                    cursor=cursor,
                    timeout_seconds=poll_timeout,
                ),
                context=context,
            )
            cursor = page.next_cursor
            terminal = False
            for item in page.items:
                record = decode_stream_record(item)
                yield record
                terminal |= is_terminal_response(record.response)
            if terminal or page.closed:
                return


def task_response(response: SendMessageResponse) -> StreamResponse:
    """Project a SendMessage result into the first A2A stream response."""

    result = StreamResponse()
    if response.HasField("task"):
        result.task.CopyFrom(response.task)
    elif response.HasField("message"):
        result.message.CopyFrom(response.message)
    else:
        raise NexusA2AOperationError(
            "SendMessage response has neither task nor message"
        )
    return result


def accepted_offset(response: SendMessageResponse) -> int:
    """Read the binding cursor returned when a message is accepted."""

    if not response.HasField("task"):
        return 0
    metadata = MessageToDict(response.task.metadata, preserving_proto_field_name=True)
    return int(metadata.get("temporal.io/accepted-offset", 0))
