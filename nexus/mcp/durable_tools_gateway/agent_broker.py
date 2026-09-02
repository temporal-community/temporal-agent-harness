"""Workflow-side bridge from the account UI to registered agents."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from a2a.types import (
    CancelTaskRequest,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
)
from google.protobuf.json_format import MessageToDict
from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.common import RetryPolicy

from .registry import REGISTRY_TASK_QUEUE, SpawnedAgentObservation

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.a2a.control import HarnessControlService
    from temporal_agent_harness.a2a.nexus import (
        HARNESS_EVENT_METADATA_KEY,
        A2AService,
        SubscribeToTaskInput,
        SubscribeToTaskItem,
    )
    from temporal_agent_harness.nexus_agent_adapter.generated import (
        ApproveToolCallInput,
        ExecuteOperatorCommandInput,
        ProvideCallbackResultInput,
        ProvideCallbackResultInputResult,
        QuerySessionInput,
    )

    from .registry_service_handler import (
        SubagentDispatchInput,
        SubagentStartInput,
        SubagentStopInput,
        subagent_proxy_activity,
        subagent_start_activity,
        subagent_stop_activity,
    )

AGENT_DISCOVERY_WORKFLOW_NAME = "BrokeredAgentDiscovery"
AGENT_ATTACH_WORKFLOW_NAME = "BrokeredAgentAttach"
MAX_ATTACH_POLLS = 500
POLL_TIMEOUT_SECONDS = 30.0
_TERMINAL_EVENT_TYPES = {
    "turn_end",
    "operator_command_completed",
    "operator_command_failed",
}


@dataclass(frozen=True)
class AgentActionInput:
    action: str
    session_id: str
    nexus_endpoint: str | None = None
    provider_url: str | None = None
    values: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentDiscoveryInput:
    session_id: str
    nexus_endpoint: str
    cursor: int = 0


@dataclass(frozen=True)
class AgentAttachInput:
    nexus_endpoint: str
    session_id: str
    stream_id: str
    from_offset: int = 0
    registry_workflow_id: str | None = None
    account_session_id: str | None = None


@dataclass(frozen=True)
class PublishBatchInput:
    stream_id: str
    items: list[SubscribeToTaskItem]
    error: str | None = None
    close: bool = False


@dataclass(frozen=True)
class PublishBatchResult:
    saw_terminal_event: bool = False
    spawned_agents: list[SpawnedAgentObservation] = field(default_factory=list)
    stopped_provider_session_ids: list[str] = field(default_factory=list)


class EventBroker:
    """In-process fan-out for a colocated gateway worker and account UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[asyncio.Queue[bytes | None]]] = {}

    def subscribe(self, stream_id: str) -> asyncio.Queue[bytes | None]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.setdefault(stream_id, set()).add(queue)
        return queue

    def unsubscribe(self, stream_id: str, queue: asyncio.Queue[bytes | None]) -> None:
        with self._lock:
            subscribers = self._subscribers.get(stream_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(stream_id, None)

    def publish(self, stream_id: str, frame: bytes | None) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.get(stream_id, ()))
        for queue in subscribers:
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # The stream is durable at the agent. A lagging browser can reconnect
                # with its last resume offset instead of back-pressuring every consumer.
                pass


event_broker = EventBroker()


def _decode_stream_item(item: SubscribeToTaskItem) -> tuple[str, dict[str, Any]]:
    response = StreamResponse()
    response.ParseFromString(base64.b64decode(item.data))
    body = response.WhichOneof("payload")
    if body is None:
        raise ValueError("A2A StreamResponse has no payload")
    a2a_metadata = MessageToDict(
        getattr(response, body).metadata, preserving_proto_field_name=True
    )
    raw = base64.b64decode(str(a2a_metadata[HARNESS_EVENT_METADATA_KEY]))
    payload = Payload()
    payload.ParseFromString(raw)
    envelope = json.loads(payload.data)
    event = envelope["event"]
    event_type = event["type"]
    return event_type, {
        **event,
        "agent_id": envelope["agent_id"],
        "turn_id": envelope["turn_id"],
        "turn_number": envelope["turn_number"],
        "timestamp": envelope["timestamp"],
        "resume_offset": item.offset + 1,
    }


def _sse(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _coalesced_ui_frames(
    items: list[SubscribeToTaskItem],
) -> list[tuple[str, dict[str, Any]]]:
    """Decode a poll page and join adjacent text deltas from the same turn.

    Provider token boundaries are not meaningful UI events. Keeping the last offset makes the
    joined frame an exact resume checkpoint while avoiding thousands of browser frames for one
    response. Non-text events retain their original one-to-one shape.
    """
    frames: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        event_type, data = _decode_stream_item(item)
        if event_type == "reply_delta" and frames and frames[-1][0] == event_type:
            prior = frames[-1][1]
            if (
                prior.get("agent_id") == data.get("agent_id")
                and prior.get("turn_id") == data.get("turn_id")
                and isinstance(prior.get("text"), str)
                and isinstance(data.get("text"), str)
            ):
                frames[-1] = (
                    event_type,
                    {
                        **prior,
                        "text": prior["text"] + data["text"],
                        "timestamp": data["timestamp"],
                        "resume_offset": data["resume_offset"],
                    },
                )
                continue
        frames.append((event_type, data))
    return frames


def _spawned_lifecycle(
    items: list[SubscribeToTaskItem],
) -> tuple[list[SpawnedAgentObservation], list[str]]:
    spawned_agents: list[SpawnedAgentObservation] = []
    stopped_source_session_ids: list[str] = []
    for item in items:
        event_type, data = _decode_stream_item(item)
        if event_type == "subagent_started":
            spawned_agents.append(
                SpawnedAgentObservation(
                    subagent_id=str(data["subagent_id"]),
                    agent_key=str(data["agent_key"]),
                    provider_session_id=str(data["workflow_id"]),
                )
            )
        elif event_type == "subagent_message_sent":
            spawned_agents.append(
                SpawnedAgentObservation(
                    subagent_id=str(data["subagent_id"]),
                    agent_key=str(data["agent_key"]),
                    provider_session_id=str(data["workflow_id"]),
                    next_expected_turn=int(data["subagent_turn"]) + 1,
                    has_started=True,
                )
            )
        elif event_type == "subagent_stopped":
            stopped_source_session_ids.append(str(data["workflow_id"]))
    return spawned_agents, stopped_source_session_ids


def _observation_dict(observation: SpawnedAgentObservation) -> dict[str, Any]:
    return {
        "subagent_id": observation.subagent_id,
        "agent_key": observation.agent_key,
        "workflow_id": observation.provider_session_id,
        "next_expected_turn": observation.next_expected_turn,
        "has_started": observation.has_started,
    }


@activity.defn
async def publish_agent_events(input: PublishBatchInput) -> PublishBatchResult:
    saw_terminal = False
    spawned_agents, stopped_provider_session_ids = _spawned_lifecycle(input.items)
    if input.error:
        event_broker.publish(
            input.stream_id,
            _sse("error", {"type": "error", "kind": "broker", "message": input.error}),
        )
    for event_type, data in _coalesced_ui_frames(input.items):
        event_broker.publish(input.stream_id, _sse(event_type, data))
        saw_terminal = saw_terminal or event_type in _TERMINAL_EVENT_TYPES
    if input.close:
        event_broker.publish(input.stream_id, None)
    return PublishBatchResult(
        saw_terminal_event=saw_terminal,
        spawned_agents=spawned_agents,
        stopped_provider_session_ids=stopped_provider_session_ids,
    )


def _values(input: AgentActionInput) -> dict[str, Any]:
    return input.values or {}


async def execute_agent_action(
    client: Client,
    input: AgentActionInput,
    *,
    execution_id: str,
) -> dict[str, Any]:
    """Execute one control request without creating a proxy workflow."""
    values = _values(input)
    if input.provider_url:
        return await _execute_external_action(
            client, input, values, execution_id=execution_id
        )
    if not input.nexus_endpoint:
        raise ValueError("nexus_endpoint is required for a native agent action")

    control_client = client.create_nexus_client(
        service=HarnessControlService, endpoint=input.nexus_endpoint
    )
    a2a_client = client.create_nexus_client(
        service=A2AService, endpoint=input.nexus_endpoint
    )
    session = QuerySessionInput(session_id=input.session_id)
    match input.action:
        case "send":
            context = {
                name: values[name]
                for name in (
                    "account_id",
                    "registered_agent_id",
                    "delegation_lineage",
                    "delegation_depth",
                    "max_delegation_depth",
                )
                if values.get(name) is not None
            }
            payload = values.get("payload") or {}
            operation = A2AService.send_message
            operation_input = SendMessageRequest(
                message=Message(
                    message_id=str(uuid.uuid4()),
                    task_id=input.session_id,
                    context_id=input.session_id,
                    role=Role.ROLE_USER,
                    parts=[Part(text=str(payload.get("text", "")))],
                    metadata={
                        "temporal.io/message-type": str(values["msg_type"]),
                        "temporal.io/payload": payload,
                    },
                ),
                metadata={
                    **context,
                    **(
                        {"expected_turn": int(values["expected_turn"])}
                        if values.get("expected_turn") is not None
                        else {}
                    ),
                },
            )
            operation_client = a2a_client
        case "close":
            operation = A2AService.cancel_task
            operation_input = CancelTaskRequest(id=input.session_id)
            operation_client = a2a_client
        case "status":
            operation = HarnessControlService.query_agent_status
            operation_input = session
            operation_client = control_client
        case "operator_interface":
            operation = HarnessControlService.query_operator_interface
            operation_input = session
            operation_client = control_client
        case "operator_command":
            kwargs: dict[str, Any] = {
                "session_id": input.session_id,
                "name": str(values["name"]),
            }
            if values.get("arg") is not None:
                kwargs["arg"] = str(values["arg"])
            operation = HarnessControlService.execute_operator_command
            operation_input = ExecuteOperatorCommandInput(**kwargs)
            operation_client = control_client
        case "approve":
            kwargs = {
                "session_id": input.session_id,
                "tool_id": str(values["tool_id"]),
                "approved": bool(values["approved"]),
                "remember": bool(values.get("remember", False)),
            }
            if values.get("reason") is not None:
                kwargs["reason"] = str(values["reason"])
            operation = HarnessControlService.approve_tool_call
            operation_input = ApproveToolCallInput(**kwargs)
            operation_client = control_client
        case "callback":
            kwargs = {
                "session_id": input.session_id,
                "tool_id": str(values["tool_id"]),
            }
            if values.get("result") is not None:
                kwargs["result"] = ProvideCallbackResultInputResult(
                    additional_properties=values["result"]
                )
            if values.get("error") is not None:
                kwargs["error"] = str(values["error"])
            operation = HarnessControlService.provide_callback_result
            operation_input = ProvideCallbackResultInput(**kwargs)
            operation_client = control_client
        case _:
            raise ValueError(f"unsupported native agent action {input.action!r}")

    result = await operation_client.execute_operation(
        operation,
        operation_input,
        id=execution_id,
        schedule_to_close_timeout=timedelta(seconds=90),
        summary=f"{input.action} agent session {input.session_id}",
    )
    if input.action == "send":
        metadata = MessageToDict(result.task.metadata, preserving_proto_field_name=True)
        return {
            "turn_number": int(metadata["temporal.io/turn-number"]),
            "turn_id": str(metadata["temporal.io/turn-id"]),
            "stream_head_offset": int(metadata["temporal.io/accepted-offset"]),
            "pending": bool(metadata["temporal.io/pending"]),
        }
    if input.action == "close":
        return {"closed": True}
    return asdict(result)


async def _execute_external_action(
    client: Client,
    input: AgentActionInput,
    values: dict[str, Any],
    *,
    execution_id: str,
) -> dict[str, Any]:
    assert input.provider_url is not None
    if input.action == "start":
        result = await client.execute_activity(
            subagent_start_activity,
            SubagentStartInput(
                url=input.provider_url,
                idempotency_key=str(values["idempotency_key"]),
            ),
            id=execution_id,
            task_queue=REGISTRY_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=5),
            summary=f"start external agent at {input.provider_url}",
        )
        return result.model_dump(mode="json")
    if input.action == "send":
        result = await client.execute_activity(
            subagent_proxy_activity,
            SubagentDispatchInput(
                url=input.provider_url,
                instance_id=input.session_id,
                msg_type=str(values["msg_type"]),
                payload=json.dumps(values.get("payload") or {}),
                expected_turn=int(values["expected_turn"]),
                idempotency_key=str(values["idempotency_key"]),
            ),
            id=execution_id,
            task_queue=REGISTRY_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=75),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            summary=f"send turn to external agent session {input.session_id}",
        )
        return result.model_dump(mode="json")
    if input.action == "close":
        await client.execute_activity(
            subagent_stop_activity,
            SubagentStopInput(
                url=input.provider_url,
                instance_id=input.session_id,
            ),
            id=execution_id,
            task_queue=REGISTRY_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=5),
            summary=f"close external agent session {input.session_id}",
        )
        return {"closed": True}
    raise ValueError(f"external agents do not support {input.action!r}")


@workflow.defn(sandboxed=False, name=AGENT_DISCOVERY_WORKFLOW_NAME)
class AgentDiscoveryWorkflow:
    """Drain retained lifecycle events and reconcile a native agent's children."""

    @workflow.run
    async def run(self, input: AgentDiscoveryInput) -> dict[str, Any]:
        a2a_client = workflow.create_nexus_client(
            service=A2AService, endpoint=input.nexus_endpoint
        )
        control_client = workflow.create_nexus_client(
            service=HarnessControlService, endpoint=input.nexus_endpoint
        )
        cursor = input.cursor
        spawned: list[SpawnedAgentObservation] = []
        stopped: list[str] = []
        closed = False
        for _ in range(100):
            poll = await a2a_client.execute_operation(
                A2AService.subscribe_to_task,
                SubscribeToTaskInput(
                    id=input.session_id,
                    cursor=cursor,
                    timeout_seconds=0.1,
                ),
            )
            batch_spawned, batch_stopped = _spawned_lifecycle(poll.items)
            spawned.extend(batch_spawned)
            stopped.extend(batch_stopped)
            cursor = poll.next_cursor
            closed = poll.closed
            if poll.closed or not poll.more_ready:
                break
        active = []
        if not closed:
            status = await control_client.execute_operation(
                HarnessControlService.query_agent_status,
                QuerySessionInput(session_id=input.session_id),
            )
            active = [asdict(item) for item in status.subagents]
        return {
            "spawned": [_observation_dict(item) for item in spawned],
            "stopped_source_session_ids": stopped,
            "next_offset": cursor,
            "active": active,
        }


@workflow.defn(sandboxed=False, name=AGENT_ATTACH_WORKFLOW_NAME)
class AgentAttachWorkflow:
    """Bounded long-poll loop for one browser attachment."""

    @workflow.run
    async def run(self, input: AgentAttachInput) -> None:
        a2a_client = workflow.create_nexus_client(
            service=A2AService, endpoint=input.nexus_endpoint
        )
        control_client = workflow.create_nexus_client(
            service=HarnessControlService, endpoint=input.nexus_endpoint
        )
        cursor = input.from_offset
        for _ in range(MAX_ATTACH_POLLS):
            try:
                poll = await a2a_client.execute_operation(
                    A2AService.subscribe_to_task,
                    SubscribeToTaskInput(
                        id=input.session_id,
                        cursor=cursor,
                        timeout_seconds=POLL_TIMEOUT_SECONDS,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - turn any Nexus failure into SSE
                await self._publish(
                    PublishBatchInput(
                        stream_id=input.stream_id,
                        items=[],
                        error=str(exc),
                        close=True,
                    )
                )
                return

            cursor = poll.next_cursor
            published = await self._publish(
                PublishBatchInput(
                    stream_id=input.stream_id,
                    items=poll.items,
                    close=poll.closed,
                )
            )
            await self._record_spawned_agents(input, published, cursor)
            if poll.closed:
                return
            if not poll.items or (published.saw_terminal_event and not poll.more_ready):
                status = await control_client.execute_operation(
                    HarnessControlService.query_agent_status,
                    QuerySessionInput(session_id=input.session_id),
                )
                if not (
                    status.turn_active
                    or status.pending_turns
                    or status.pending_approvals
                    or status.pending_callbacks
                ):
                    await self._publish(
                        PublishBatchInput(
                            stream_id=input.stream_id, items=[], close=True
                        )
                    )
                    return

        workflow.continue_as_new(
            AgentAttachInput(
                nexus_endpoint=input.nexus_endpoint,
                session_id=input.session_id,
                stream_id=input.stream_id,
                from_offset=cursor,
                registry_workflow_id=input.registry_workflow_id,
                account_session_id=input.account_session_id,
            )
        )

    async def _record_spawned_agents(
        self,
        input: AgentAttachInput,
        published: PublishBatchResult,
        next_offset: int,
    ) -> None:
        if not input.registry_workflow_id or not input.account_session_id:
            return
        registry = workflow.get_external_workflow_handle(input.registry_workflow_id)
        await registry.signal(
            "record_spawned_agent_batch",
            args=[
                input.account_session_id,
                published.spawned_agents,
                published.stopped_provider_session_ids,
                next_offset,
            ],
        )

    async def _publish(self, input: PublishBatchInput) -> PublishBatchResult:
        return await workflow.execute_activity(
            publish_agent_events,
            input,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
