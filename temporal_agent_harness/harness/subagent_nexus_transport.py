# ABOUTME: A2A transport for a harness subagent reached directly through Nexus.

from __future__ import annotations

import base64
from typing import Any

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from a2a.types import (
        CancelTaskRequest,
        Message,
        Part,
        Role,
        SendMessageRequest,
        StreamResponse,
    )
    from google.protobuf.json_format import MessageToDict
    from nexus_a2a import (
        A2AService,
        SubscribeToTaskInput,
        SubscribeToTaskItem,
    )

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


def _decode_stream_item(item: SubscribeToTaskItem) -> AgentEvent:
    """Recover the rich harness event carried as an A2A extension."""

    response = StreamResponse()
    response.ParseFromString(base64.b64decode(item.data))
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


class NexusTransport:
    """Reach a harness subagent through the A2A Nexus protocol binding."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._start_configs: dict[str, AgentConfig] = {}

    def _client(self) -> workflow.NexusClient[A2AService]:
        return workflow.create_nexus_client(service=A2AService, endpoint=self._endpoint)

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
        config = self._start_configs.get(target, AgentConfig())
        context: dict[str, Any] = {"expected_turn": expected_turn}
        if config.account_id is not None:
            context["account_id"] = config.account_id
        if config.registered_agent_id is not None:
            context["registered_agent_id"] = config.registered_agent_id
        if config.delegation_lineage is not None:
            context["delegation_lineage"] = list(config.delegation_lineage)
        if config.delegation_depth is not None:
            context["delegation_depth"] = config.delegation_depth
        if config.max_delegation_depth is not None:
            context["max_delegation_depth"] = config.max_delegation_depth

        try:
            sent = await self._client().execute_operation(
                A2AService.send_message,
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
                    metadata=context,
                ),
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

        cursor = from_offset
        output: dict[str, Any] = {}
        while True:
            poll = await self._client().execute_operation(
                A2AService.subscribe_to_task,
                SubscribeToTaskInput(
                    id=target,
                    cursor=cursor,
                    timeout_seconds=POLL_TIMEOUT_SECONDS,
                ),
            )
            if poll.closed:
                raise ApplicationError(
                    f"subagent {target!r} closed before turn {turn_number} replied",
                    {"subagent_turn": turn_number},
                    type="SubagentNoReply",
                    non_retryable=True,
                )
            for item in poll.items:
                cursor = item.offset + 1
                event = _decode_stream_item(item)
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
                        consumed_offset=cursor,
                    )

    async def stop(self, *, target: str) -> None:
        await self._client().execute_operation(
            A2AService.cancel_task, CancelTaskRequest(id=target)
        )
        self._start_configs.pop(target, None)


def _rejection_error(exc: Exception) -> ApplicationError:
    message = str(exc)
    if message.startswith("StaleTurn: "):
        return ApplicationError(message, type="StaleTurn", non_retryable=True)
    if message.startswith("AgentBusy: "):
        return ApplicationError(message, type="AgentBusy", non_retryable=True)
    return ApplicationError(message, type="SubagentSendRejected", non_retryable=True)
