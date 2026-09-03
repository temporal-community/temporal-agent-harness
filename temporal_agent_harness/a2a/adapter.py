"""Expose a Temporal Agent Harness workflow as an A2A agent over Nexus."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    CancelTaskRequest,
    GetExtendedAgentCardRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    SendMessageRequest,
    SendMessageResponse,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from nexus_a2a import (
    A2A_NEXUS_BINDING,
    A2A_PROTOCOL_VERSION,
    A2AService,
    SubscribeToTaskInput,
    SubscribeToTaskOutput,
)
from nexusrpc import HandlerError, HandlerErrorType
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from temporalio import nexus
from temporalio.client import Client
from temporalio.service import RPCError

from temporal_agent_harness.harness.agent_client import AgentClient, StaleTurnError
from temporal_agent_harness.harness.agent_protocol import AgentConfig

from .stream import (
    A2A_STREAM_POLL_UPDATE,
)

HARNESS_HANDLER_METADATA_KEY = "temporal.io/message-type"
HARNESS_PAYLOAD_METADATA_KEY = "temporal.io/payload"
_MAX_SEND_RETRIES = 5


@dataclass(frozen=True)
class A2AHandlerConfig:
    agent_task_queue: str
    workflow_name: str
    workflow_id_prefix: str
    is_message_queuing_enabled: bool
    agent_card: AgentCard
    start_missing_tasks: bool = True


def make_agent_card(
    *,
    name: str,
    description: str,
    endpoint: str,
    skills: tuple[tuple[str, str], ...] = (("ask", "Ask the agent a question."),),
) -> AgentCard:
    """Build the public A2A card for a harness-native Nexus endpoint."""

    return AgentCard(
        name=name,
        description=description,
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                url=endpoint,
                protocol_binding=A2A_NEXUS_BINDING,
                protocol_version=A2A_PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id=skill_id,
                name=skill_id,
                description=skill_description,
                tags=["temporal-agent-harness"],
                input_modes=["text/plain", "application/json"],
                output_modes=["text/plain", "application/json"],
            )
            for skill_id, skill_description in skills
        ],
    )


def _timestamp(value: float | None = None) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(
        datetime.fromtimestamp(value, UTC) if value is not None else datetime.now(UTC)
    )
    return timestamp


def _metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return MessageToDict(value, preserving_proto_field_name=True)


def _message_text(message: Message) -> str:
    return "".join(part.text for part in message.parts if part.HasField("text"))


def _is_completed(exc: Exception) -> bool:
    return "already completed" in str(exc).lower()


def _is_not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return "workflow not found" in message or "not found for id" in message


@service_handler(service=A2AService)
class A2AServiceHandler:
    """A2A v1 adapter around the harness's durable workflow protocol."""

    def __init__(self, client: Client, config: A2AHandlerConfig) -> None:
        self._client = client
        self._config = config

    def _workflow_id(self, task_id: str) -> str:
        return self._config.workflow_id_prefix + task_id

    def _agent_client(self, task_id: str) -> AgentClient:
        return AgentClient(self._client, self._workflow_id(task_id))

    @sync_operation
    async def send_message(
        self, ctx: StartOperationContext, request: SendMessageRequest
    ) -> SendMessageResponse:
        message = request.message
        task_id = message.task_id
        if not task_id:
            raise HandlerError(
                "A2A Message.task_id is required by the Nexus binding",
                type=HandlerErrorType.BAD_REQUEST,
            )
        metadata = _metadata(request.metadata)
        message_metadata = _metadata(message.metadata)
        msg_type = str(message_metadata.get(HARNESS_HANDLER_METADATA_KEY, "ask"))
        payload = message_metadata.get(HARNESS_PAYLOAD_METADATA_KEY)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if not isinstance(payload, dict):
            payload = {"text": _message_text(message)}

        config = AgentConfig(
            is_message_queuing_enabled=self._config.is_message_queuing_enabled,
        )
        expected = metadata.get("expected_turn")
        expected_turn = int(expected) if expected is not None else 1
        client = self._agent_client(task_id)
        attempts = 1 if expected is not None else _MAX_SEND_RETRIES
        for attempt in range(attempts):
            if attempt:
                status = await client.get_status()
                expected_turn = status.current_turn + len(status.pending_turns) + 1
            try:
                if self._config.start_missing_tasks:
                    reply = await client.start_and_submit_message(
                        msg_type,
                        payload,
                        expected_turn,
                        workflow_name=self._config.workflow_name,
                        task_queue=self._config.agent_task_queue,
                        start_config=config,
                        update_id=f"a2a-send-{ctx.request_id}-{attempt}",
                    )
                else:
                    reply = await client.submit_message(
                        msg_type,
                        payload,
                        expected_turn,
                        update_id=f"a2a-send-{ctx.request_id}-{attempt}",
                    )
                break
            except StaleTurnError as exc:
                if expected is not None:
                    raise HandlerError(
                        f"StaleTurn: {exc}", type=HandlerErrorType.BAD_REQUEST
                    ) from exc
                await asyncio.sleep((attempt + 1) * 0.05)
        else:
            raise HandlerError(
                "SendMessage exhausted retries", type=HandlerErrorType.INTERNAL
            )

        task = Task(
            id=task_id,
            context_id=message.context_id or task_id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_WORKING, timestamp=_timestamp()
            ),
            history=[message],
            metadata={
                "temporal.io/turn-number": reply.turn_number,
                "temporal.io/turn-id": reply.turn_id,
                "temporal.io/accepted-offset": reply.accepted_offset,
                "temporal.io/pending": reply.pending,
            },
        )
        return SendMessageResponse(task=task)

    @sync_operation
    async def get_task(
        self, _ctx: StartOperationContext, request: GetTaskRequest
    ) -> Task:
        try:
            status = await self._agent_client(request.id).get_status()
        except RPCError as exc:
            if _is_not_found(exc):
                return Task(
                    id=request.id,
                    context_id=request.id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_SUBMITTED,
                        timestamp=_timestamp(),
                    ),
                )
            if _is_completed(exc):
                return Task(
                    id=request.id,
                    context_id=request.id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_COMPLETED,
                        timestamp=_timestamp(),
                    ),
                )
            raise
        state = (
            TaskState.TASK_STATE_WORKING
            if status.turn_active
            or status.pending_turns
            or status.pending_approvals
            or status.pending_callbacks
            else TaskState.TASK_STATE_INPUT_REQUIRED
        )
        return Task(
            id=request.id,
            context_id=request.id,
            status=TaskStatus(state=state, timestamp=_timestamp()),
            metadata={
                "temporal.io/current-turn": status.current_turn,
                "temporal.io/subagents": [
                    {
                        "subagent_id": item.subagent_id,
                        "agent_key": item.agent_key,
                        "workflow_id": item.workflow_id,
                        "next_expected_turn": item.next_expected_turn,
                    }
                    for item in status.subagents
                ],
                "temporal.io/pending-approvals": len(status.pending_approvals),
                "temporal.io/pending-callbacks": len(status.pending_callbacks),
            },
        )

    @sync_operation
    async def list_tasks(
        self, _ctx: StartOperationContext, _request: ListTasksRequest
    ) -> ListTasksResponse:
        # Task enumeration belongs to the account registry. An agent endpoint has no
        # account-wide visibility and must not leak workflows from another tenant.
        return ListTasksResponse(tasks=[], page_size=0, total_size=0)

    @sync_operation
    async def cancel_task(
        self, _ctx: StartOperationContext, request: CancelTaskRequest
    ) -> Task:
        await self._agent_client(request.id).close()
        return Task(
            id=request.id,
            context_id=request.id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_CANCELED, timestamp=_timestamp()
            ),
        )

    @sync_operation
    async def get_extended_agent_card(
        self, _ctx: StartOperationContext, _request: GetExtendedAgentCardRequest
    ) -> AgentCard:
        result = AgentCard()
        result.CopyFrom(self._config.agent_card)
        return result

    @nexus.temporal_operation
    async def subscribe_to_task(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        request: SubscribeToTaskInput,
    ) -> nexus.TemporalOperationResult[SubscribeToTaskOutput]:
        workflow_id = self._workflow_id(request.id)
        try:
            result = await client.start_workflow_update(
                workflow_id,
                A2A_STREAM_POLL_UPDATE,
                request,
                result_type=SubscribeToTaskOutput,
            )
        except RPCError as exc:
            if _is_completed(exc):
                return nexus.TemporalOperationResult.sync(
                    SubscribeToTaskOutput(
                        items=[], next_cursor=request.cursor, closed=True
                    )
                )
            if _is_not_found(exc):
                return nexus.TemporalOperationResult.sync(
                    SubscribeToTaskOutput(
                        items=[], next_cursor=request.cursor, closed=True
                    )
                )
            raise
        if result.token is not None:
            return nexus.TemporalOperationResult.async_token(result.token)
        return nexus.TemporalOperationResult.sync(result.value)
