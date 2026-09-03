from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    Message,
    Part,
    Role,
    SendMessageRequest,
    SendMessageResponse,
    StreamResponse,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from nexus_a2a import (
    A2A_NEXUS_BINDING,
    A2AService,
    NexusA2AClient,
    NexusA2AClientTransport,
    StandaloneNexusExecutor,
    SubscribeToTaskItem,
    SubscribeToTaskOutput,
    create_nexus_a2a_client,
    register_nexus_a2a_transport,
)


def _card() -> AgentCard:
    return AgentCard(
        name="Probe",
        description="Probe agent",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url="probe-endpoint",
                protocol_binding=A2A_NEXUS_BINDING,
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[],
    )


def _request() -> SendMessageRequest:
    return SendMessageRequest(
        message=Message(
            message_id="message-1",
            task_id="task-1",
            context_id="task-1",
            role=Role.ROLE_USER,
            parts=[Part(text="hello")],
        )
    )


def _encoded(response: StreamResponse) -> str:
    return base64.b64encode(response.SerializeToString()).decode()


class _FakeNexusClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def execute_operation(
        self, operation: Any, request: Any, **kwargs: Any
    ) -> Any:
        self.calls.append((operation.name, request, kwargs))
        if operation is A2AService.send_message:
            return SendMessageResponse(
                task=Task(
                    id="task-1",
                    context_id="task-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                    metadata={"temporal.io/accepted-offset": 4},
                )
            )
        if operation is A2AService.subscribe_to_task:
            reply = StreamResponse(
                message=Message(
                    message_id="reply-1",
                    task_id="task-1",
                    context_id="task-1",
                    role=Role.ROLE_AGENT,
                    parts=[Part(text="hello back")],
                )
            )
            done = StreamResponse(
                status_update=TaskStatusUpdateEvent(
                    task_id="task-1",
                    context_id="task-1",
                    status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
                )
            )
            return SubscribeToTaskOutput(
                items=[
                    SubscribeToTaskItem(offset=4, data=_encoded(reply)),
                    SubscribeToTaskItem(offset=5, data=_encoded(done)),
                ],
                next_cursor=6,
            )
        raise AssertionError(f"unexpected operation {operation.name}")


class _FakeTemporalClient:
    def __init__(self) -> None:
        self.nexus = _FakeNexusClient()
        self.created: list[tuple[type[Any], str]] = []

    def create_nexus_client(
        self, service: type[Any], endpoint: str
    ) -> _FakeNexusClient:
        self.created.append((service, endpoint))
        return self.nexus


async def _collect(stream: AsyncIterator[StreamResponse]) -> list[StreamResponse]:
    return [item async for item in stream]


async def test_transport_maps_a2a_stream_to_standalone_nexus_operations() -> None:
    temporal_client = _FakeTemporalClient()
    transport = NexusA2AClientTransport(
        NexusA2AClient(
            StandaloneNexusExecutor(temporal_client),  # type: ignore[arg-type]
            "probe-endpoint",
            poll_timeout_seconds=1,
        ),
        _card(),
    )

    responses = await _collect(transport.send_message_streaming(_request()))

    assert [response.WhichOneof("payload") for response in responses] == [
        "task",
        "message",
        "status_update",
    ]
    assert temporal_client.created == [
        (A2AService, "probe-endpoint"),
        (A2AService, "probe-endpoint"),
    ]
    assert [call[0] for call in temporal_client.nexus.calls] == [
        "SendMessage",
        "SubscribeToTask",
    ]
    subscription = temporal_client.nexus.calls[1][1]
    assert subscription.cursor == 4
    assert temporal_client.nexus.calls[0][2]["id"] == "message-1"
    assert temporal_client.nexus.calls[1][2]["id"].startswith("a2a-")


async def test_official_a2a_client_uses_registered_nexus_transport() -> None:
    temporal_client = _FakeTemporalClient()
    factory = ClientFactory(
        ClientConfig(supported_protocol_bindings=[A2A_NEXUS_BINDING])
    )
    register_nexus_a2a_transport(factory, temporal_client)  # type: ignore[arg-type]
    client = factory.create(_card())

    responses = await _collect(client.send_message(_request()))

    assert responses[0].HasField("task")
    assert responses[1].HasField("message")


async def test_convenience_client_uses_endpoint_from_agent_card() -> None:
    temporal_client = _FakeTemporalClient()
    client = create_nexus_a2a_client(  # type: ignore[arg-type]
        temporal_client, _card(), poll_timeout_seconds=1
    )

    responses = await _collect(client.send_message(_request()))

    assert responses[1].message.parts[0].text == "hello back"
    assert temporal_client.created == [
        (A2AService, "probe-endpoint"),
        (A2AService, "probe-endpoint"),
    ]
