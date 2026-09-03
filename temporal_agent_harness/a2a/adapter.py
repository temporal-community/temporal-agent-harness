"""Adapt Temporal Agent Harness workflows to the generic Nexus A2A backend."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from a2a.types import (
    AgentCard,
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
    A2ABackendError,
    BackendErrorKind,
    OperationContext,
    SubscribeToTaskInput,
    SubscribeToTaskOutput,
)
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
class HarnessA2ABackendConfig:
    """Settings for exposing one harness workflow type as A2A tasks."""

    agent_task_queue: str
    workflow_name: str
    workflow_id_prefix: str
    is_message_queuing_enabled: bool
    agent_card: AgentCard
    start_missing_tasks: bool = True


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
    """True when the target agent workflow has already finished (see subscribe_to_task)."""

    return "already completed" in str(exc).lower()


def _is_not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return "workflow not found" in message or "not found for id" in message


class HarnessA2ABackend:
    """Implement A2A task semantics using Temporal Agent Harness workflows.

    A caller sends an A2A Message to deliver user input and repeatedly subscribes to the task
    to consume the agent's rich response stream. Harness-only operator and approval controls
    deliberately live in the separate ``HarnessControlService``.
    """

    def __init__(self, client: Client, config: HarnessA2ABackendConfig) -> None:
        self._client = client
        self._config = config

    def _workflow_id(self, task_id: str) -> str:
        return self._config.workflow_id_prefix + task_id

    def _agent_client(self, task_id: str) -> AgentClient:
        """Cheap to construct per-call; durable state remains in the target workflow."""
        return AgentClient(self._client, self._workflow_id(task_id))

    # -----------------------------------------------------------------------
    # SendMessage — AgentClient.start_and_submit_message()'s guess-and-retry caller
    # -----------------------------------------------------------------------

    async def send_message(
        self, context: OperationContext, request: SendMessageRequest
    ) -> SendMessageResponse:
        message = request.message
        task_id = message.task_id
        if not task_id:
            raise A2ABackendError(
                "A2A Message.task_id is required by the Nexus binding",
                kind=BackendErrorKind.BAD_REQUEST,
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
        # A generic A2A caller may not know expected_turn; guess 1, then re-derive from status on
        # retry. A harness-aware caller can provide the exact value in request metadata.
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
                        update_id=f"a2a-send-{context.request_id}-{attempt}",
                    )
                else:
                    reply = await client.submit_message(
                        msg_type,
                        payload,
                        expected_turn,
                        update_id=f"a2a-send-{context.request_id}-{attempt}",
                    )
                break
            except StaleTurnError as exc:
                if expected is not None:
                    raise A2ABackendError(
                        f"StaleTurn: {exc}", kind=BackendErrorKind.BAD_REQUEST
                    ) from exc
                await asyncio.sleep((attempt + 1) * 0.05)
        else:
            raise A2ABackendError(
                "SendMessage exhausted retries", kind=BackendErrorKind.INTERNAL
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

    async def get_task(
        self, _context: OperationContext, request: GetTaskRequest
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

    async def list_tasks(
        self, _context: OperationContext, _request: ListTasksRequest
    ) -> ListTasksResponse:
        # Task enumeration belongs to the account registry. An agent endpoint has no
        # account-wide visibility and must not leak workflows from another tenant.
        return ListTasksResponse(tasks=[], page_size=0, total_size=0)

    async def cancel_task(
        self, _context: OperationContext, request: CancelTaskRequest
    ) -> Task:
        await self._agent_client(request.id).close()
        return Task(
            id=request.id,
            context_id=request.id,
            status=TaskStatus(
                state=TaskState.TASK_STATE_CANCELED, timestamp=_timestamp()
            ),
        )

    async def get_extended_agent_card(
        self, _context: OperationContext, _request: GetExtendedAgentCardRequest
    ) -> AgentCard:
        result = AgentCard()
        result.CopyFrom(self._config.agent_card)
        return result

    # -----------------------------------------------------------------------
    # SubscribeToTask — async operation backed by the A2A stream-poll update
    # -----------------------------------------------------------------------

    async def subscribe_to_task(
        self,
        _context: OperationContext,
        client: nexus.TemporalNexusClient,
        request: SubscribeToTaskInput,
    ) -> nexus.TemporalOperationResult[SubscribeToTaskOutput]:
        """Long-poll the agent's WorkflowStream through update-with-callback.

        The workflow-side A2A update projects the retained harness events into an A2A page before
        the callback completes. Return ``closed=True`` synchronously when the target workflow is
        absent or already completed.
        """
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
