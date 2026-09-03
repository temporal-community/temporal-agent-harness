"""Official A2A Python SDK frontend for the Nexus transport binding."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Sequence
from typing import TypeVar

from a2a.client import (
    BaseClient,
    Client,
    ClientCallContext,
    ClientConfig,
    ClientFactory,
)
from a2a.client.errors import A2AClientError
from a2a.client.interceptors import ClientCallInterceptor
from a2a.client.transports import ClientTransport
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    DeleteTaskPushNotificationConfigRequest,
    GetExtendedAgentCardRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    ListTaskPushNotificationConfigsResponse,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    SendMessageResponse,
    StreamResponse,
    SubscribeToTaskRequest,
    Task,
    TaskPushNotificationConfig,
)
from temporalio.client import Client as TemporalClient

from nexus_a2a.client import (
    DEFAULT_POLL_TIMEOUT_SECONDS,
    NexusA2AClient,
    accepted_offset,
    task_response,
)
from nexus_a2a.context import RequestContext
from nexus_a2a.errors import NexusA2AError
from nexus_a2a.execution import StandaloneNexusExecutor
from nexus_a2a.service import A2A_NEXUS_BINDING

_ResultT = TypeVar("_ResultT")


def _request_context(
    context: ClientCallContext | None,
    *,
    idempotency_key: str | None = None,
) -> RequestContext:
    return RequestContext(
        idempotency_key=idempotency_key,
        timeout_seconds=context.timeout if context is not None else None,
    )


async def _translate(awaitable: Awaitable[_ResultT]) -> _ResultT:
    try:
        return await awaitable
    except NexusA2AError as exc:
        raise A2AClientError(str(exc)) from exc


class NexusA2AClientTransport(ClientTransport):
    """Adapt the official A2A client API to the shared Nexus A2A client."""

    def __init__(self, client: NexusA2AClient, agent_card: AgentCard) -> None:
        self.agent_card = agent_card
        self.endpoint = client.endpoint
        self._client = client
        self._closed = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise A2AClientError("Nexus A2A transport is closed")

    async def _stream(
        self,
        *,
        task_id: str,
        tenant: str,
        cursor: int,
        context: ClientCallContext | None,
    ) -> AsyncGenerator[StreamResponse]:
        try:
            async for record in self._client.stream_task(
                task_id=task_id,
                tenant=tenant,
                cursor=cursor,
                context=_request_context(context),
            ):
                yield record.response
        except NexusA2AError as exc:
            raise A2AClientError(str(exc)) from exc

    async def send_message(
        self,
        request: SendMessageRequest,
        *,
        context: ClientCallContext | None = None,
    ) -> SendMessageResponse:
        self._ensure_open()
        return await _translate(
            self._client.send_message(
                request,
                context=_request_context(
                    context, idempotency_key=request.message.message_id
                ),
            )
        )

    async def send_message_streaming(
        self,
        request: SendMessageRequest,
        *,
        context: ClientCallContext | None = None,
    ) -> AsyncGenerator[StreamResponse]:
        sent = await self.send_message(request, context=context)
        yield task_response(sent)
        if not sent.HasField("task"):
            return
        async for response in self._stream(
            task_id=sent.task.id,
            tenant=request.tenant,
            cursor=accepted_offset(sent),
            context=context,
        ):
            yield response

    async def get_task(self, request: GetTaskRequest, *, context=None) -> Task:
        self._ensure_open()
        return await _translate(
            self._client.get_task(request, context=_request_context(context))
        )

    async def list_tasks(
        self, request: ListTasksRequest, *, context=None
    ) -> ListTasksResponse:
        self._ensure_open()
        return await _translate(
            self._client.list_tasks(request, context=_request_context(context))
        )

    async def cancel_task(self, request: CancelTaskRequest, *, context=None) -> Task:
        self._ensure_open()
        return await _translate(
            self._client.cancel_task(request, context=_request_context(context))
        )

    async def subscribe(
        self, request: SubscribeToTaskRequest, *, context=None
    ) -> AsyncGenerator[StreamResponse]:
        self._ensure_open()
        async for response in self._stream(
            task_id=request.id,
            tenant=request.tenant,
            cursor=0,
            context=context,
        ):
            yield response

    async def get_extended_agent_card(
        self, request: GetExtendedAgentCardRequest, *, context=None
    ) -> AgentCard:
        self._ensure_open()
        return await _translate(
            self._client.get_extended_agent_card(
                request, context=_request_context(context)
            )
        )

    async def create_task_push_notification_config(
        self, request: TaskPushNotificationConfig, *, context=None
    ) -> TaskPushNotificationConfig:
        raise NotImplementedError("Nexus A2A does not support push notifications")

    async def get_task_push_notification_config(
        self, request: GetTaskPushNotificationConfigRequest, *, context=None
    ) -> TaskPushNotificationConfig:
        raise NotImplementedError("Nexus A2A does not support push notifications")

    async def list_task_push_notification_configs(
        self, request: ListTaskPushNotificationConfigsRequest, *, context=None
    ) -> ListTaskPushNotificationConfigsResponse:
        raise NotImplementedError("Nexus A2A does not support push notifications")

    async def delete_task_push_notification_config(
        self, request: DeleteTaskPushNotificationConfigRequest, *, context=None
    ) -> None:
        raise NotImplementedError("Nexus A2A does not support push notifications")

    async def close(self) -> None:
        self._closed = True


def _core_client(
    temporal_client: TemporalClient,
    endpoint: str,
    poll_timeout_seconds: float,
) -> NexusA2AClient:
    return NexusA2AClient(
        StandaloneNexusExecutor(temporal_client),
        endpoint,
        poll_timeout_seconds=poll_timeout_seconds,
    )


def register_nexus_a2a_transport(
    factory: ClientFactory,
    temporal_client: TemporalClient,
    *,
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
) -> ClientFactory:
    """Teach an official A2A ``ClientFactory`` how to call Nexus endpoints."""

    def produce(
        card: AgentCard, endpoint: str, _config: ClientConfig
    ) -> ClientTransport:
        return NexusA2AClientTransport(
            _core_client(temporal_client, endpoint, poll_timeout_seconds), card
        )

    factory.register(A2A_NEXUS_BINDING, produce)
    return factory


def create_nexus_a2a_client(
    temporal_client: TemporalClient,
    agent_card: AgentCard,
    *,
    endpoint: str | None = None,
    config: ClientConfig | None = None,
    interceptors: Sequence[ClientCallInterceptor] = (),
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
) -> Client:
    """Create an official A2A client for a Temporal Nexus agent endpoint."""

    if endpoint is None:
        interface = next(
            (
                candidate
                for candidate in agent_card.supported_interfaces
                if candidate.protocol_binding == A2A_NEXUS_BINDING
            ),
            None,
        )
        if interface is None:
            raise ValueError(f"agent card has no {A2A_NEXUS_BINDING!r} interface")
        endpoint = interface.url
    client_config = config or ClientConfig(
        streaming=True,
        supported_protocol_bindings=[A2A_NEXUS_BINDING],
    )
    return BaseClient(
        agent_card,
        client_config,
        NexusA2AClientTransport(
            _core_client(temporal_client, endpoint, poll_timeout_seconds), agent_card
        ),
        list(interceptors),
    )
