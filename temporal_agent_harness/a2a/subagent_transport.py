# ABOUTME: Harness subagent adapter for agents reached through Nexus A2A.

from __future__ import annotations

import base64
from typing import Any

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from a2a.types import CancelTaskRequest, Message, Part, Role, SendMessageRequest
    from google.protobuf.json_format import MessageToDict
    from nexus_a2a import NexusA2AClient, StreamRecord, WorkflowNexusExecutor

    from temporal_agent_harness.a2a.stream import HARNESS_EVENT_METADATA_KEY
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
    SubagentMessageSent,
    SubagentTurnResult,
)
from temporal_agent_harness.harness.agent_workflow import _current_runner
from temporal_agent_harness.harness.stream_context import TurnStreamContext

POLL_TIMEOUT_SECONDS = 25.0


def _decode_harness_event(record: StreamRecord) -> AgentEvent:
    """Recover the rich harness extension from a standard A2A stream event."""

    response = record.response
    body = response.WhichOneof("payload")
    if body is None:
        raise ValueError("A2A StreamResponse has no payload")
    metadata = MessageToDict(
        getattr(response, body).metadata, preserving_proto_field_name=True
    )
    encoded_payload = str(metadata[HARNESS_EVENT_METADATA_KEY])
    payload = Payload()
    payload.ParseFromString(base64.b64decode(encoded_payload))
    return workflow.payload_converter().from_payload(payload, AgentEvent)


class NexusA2ASubagentTransport:
    """Adapt the shared Nexus A2A client to the harness subagent contract."""

    def __init__(self, endpoint: str) -> None:
        self._client = NexusA2AClient(
            WorkflowNexusExecutor(),
            endpoint,
            poll_timeout_seconds=POLL_TIMEOUT_SECONDS,
        )
        self._start_configs: dict[str, AgentConfig] = {}

    async def start(self, *, agent_key: str, config: AgentConfig) -> str:
        target = f"{agent_key}-subagent-{workflow.uuid4()}"
        self._start_configs[target] = config
        return target

    async def dispatch(
        self,
        *,
        target: str,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        from_offset: int,
        handle: str,
        agent_key: str,
        parent_stream_context: TurnStreamContext,
    ) -> SubagentTurnResult:
        del parent_stream_context
        try:
            sent = await self._client.send_message(
                SendMessageRequest(
                    message=Message(
                        message_id=str(workflow.uuid4()),
                        task_id=target,
                        context_id=target,
                        role=Role.ROLE_USER,
                        parts=[Part(text=str(payload.get("text", "")))],
                        metadata={
                            "temporal.io/message-type": msg_type,
                            "temporal.io/payload": payload,
                        },
                    ),
                    metadata={"expected_turn": expected_turn},
                )
            )
        except Exception as exc:
            raise _rejection_error(exc) from exc

        sent_metadata = MessageToDict(
            sent.task.metadata, preserving_proto_field_name=True
        )
        turn_number = int(sent_metadata["temporal.io/turn-number"])
        turn_id = str(sent_metadata["temporal.io/turn-id"])
        _current_runner().publish(
            SubagentMessageSent(
                subagent_id=handle,
                agent_key=agent_key,
                workflow_id=target,
                function=msg_type,
                subagent_turn=turn_number,
                from_offset=from_offset,
            )
        )

        output: dict[str, Any] = {}
        async for record in self._client.stream_task(
            task_id=target,
            cursor=from_offset,
        ):
            event = _decode_harness_event(record)
            if event.turn_id != turn_id:
                continue
            envelope = event.event
            if envelope.type == AgentEventType.ERROR:
                raise ApplicationError(
                    envelope.message or "subagent turn failed",
                    {"subagent_turn": turn_number},
                    type="SubagentTurnError",
                    non_retryable=True,
                )
            if envelope.type == AgentEventType.REPLY:
                output = envelope.output
            if envelope.type == AgentEventType.TURN_END:
                return SubagentTurnResult(
                    output=output,
                    turn_id=turn_id,
                    turn_number=turn_number,
                    consumed_offset=record.offset + 1,
                )
        raise ApplicationError(
            f"subagent {target!r} closed before turn {turn_number} replied",
            {"subagent_turn": turn_number},
            type="SubagentNoReply",
            non_retryable=True,
        )

    async def stop(self, *, target: str) -> None:
        await self._client.cancel_task(CancelTaskRequest(id=target))
        self._start_configs.pop(target, None)


def _rejection_error(exc: Exception) -> ApplicationError:
    """Map a Nexus rejection to the transport-independent error type."""

    current: BaseException | None = exc
    messages: list[str] = []
    while current is not None:
        messages.append(str(current))
        current = current.__cause__
    message = ": ".join(messages)
    if "StaleTurn:" in message:
        return ApplicationError(message, type="StaleTurn", non_retryable=True)
    if "AgentBusy:" in message:
        return ApplicationError(message, type="AgentBusy", non_retryable=True)
    return ApplicationError(message, type="SubagentSendRejected", non_retryable=True)
