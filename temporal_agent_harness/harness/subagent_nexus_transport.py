# ABOUTME: Transport for a harness subagent reached directly through Nexus.

from __future__ import annotations

import base64
import json
from typing import Any

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
    SubagentMessageSent,
    SubagentTurnResult,
)
from temporal_agent_harness.harness.agent_workflow import _current_runner
from temporal_agent_harness.harness.stream_context import TurnStreamContext
from temporal_agent_harness.nexus_agent_adapter.generated import (
    AgentService,
    PollMessagesInput,
    QuerySessionInput,
    SendAgentMessageInput,
)

# Requested wait for one poll request.
POLL_TIMEOUT_SECONDS = 25.0


def _decode_stream_item(data: str) -> AgentEvent:
    """Decode an event from a base64-encoded Temporal payload."""
    payload = Payload()
    payload.ParseFromString(base64.b64decode(data))
    return workflow.payload_converter().from_payload(payload, AgentEvent)


class NexusTransport:
    """Reach a harness subagent through its Nexus AgentService endpoint.

    The target service creates its own ``AgentConfig``. It cannot use the parent handle as
    the remote agent ID. A client therefore cannot join the two streams by agent ID.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint
        self._start_configs: dict[str, AgentConfig] = {}

    def _client(self) -> workflow.NexusClient[AgentService]:
        return workflow.create_nexus_client(
            service=AgentService, endpoint=self._endpoint
        )

    async def start(self, *, agent_key: str, config: AgentConfig) -> str:
        # The first message starts or reuses the target workflow.
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
        try:
            config = self._start_configs.get(target, AgentConfig())
            context: dict[str, Any] = {}
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
            sent = await self._client().execute_operation(
                AgentService.send_agent_message,
                SendAgentMessageInput(
                    session_id=target,
                    msg_type=msg_type,
                    payload=json.dumps(payload),
                    expected_turn=expected_turn,
                    **context,
                ),
            )
        except Exception as exc:
            raise _rejection_error(exc) from exc

        # Publish after Nexus accepts the message.
        _current_runner().publish(
            SubagentMessageSent(
                subagent_id=handle,
                agent_key=agent_key,
                workflow_id=target,
                function=msg_type,
                subagent_turn=sent.turn_number,
                from_offset=from_offset,
            )
        )

        cursor = from_offset
        output: dict[str, Any] = {}
        while True:
            poll = await self._client().execute_operation(
                AgentService.poll_messages,
                PollMessagesInput(
                    session_id=target,
                    cursor=cursor,
                    timeout_seconds=POLL_TIMEOUT_SECONDS,
                ),
            )
            if poll.closed:
                raise ApplicationError(
                    f"subagent {target!r} closed before turn {sent.turn_number} replied",
                    {"subagent_turn": sent.turn_number},
                    type="SubagentNoReply",
                    non_retryable=True,
                )
            for item in poll.items:
                cursor = item.offset + 1
                event = _decode_stream_item(item.data)
                if event.turn_id != sent.turn_id:
                    continue
                envelope = event.event
                if envelope.type == AgentEventType.ERROR:
                    raise ApplicationError(
                        envelope.message or "subagent turn failed",
                        {"subagent_turn": sent.turn_number},
                        type="SubagentTurnError",
                        non_retryable=True,
                    )
                if envelope.type == AgentEventType.REPLY:
                    output = envelope.output
                if envelope.type == AgentEventType.TURN_END:
                    return SubagentTurnResult(
                        output=output,
                        turn_id=sent.turn_id,
                        turn_number=sent.turn_number,
                        consumed_offset=cursor,
                    )

    async def stop(self, *, target: str) -> None:
        await self._client().execute_operation(
            AgentService.close_session, QuerySessionInput(session_id=target)
        )
        self._start_configs.pop(target, None)


def _rejection_error(exc: Exception) -> ApplicationError:
    """Map a Nexus rejection to the transport-independent error type."""
    message = str(exc)
    if message.startswith("StaleTurn: "):
        return ApplicationError(message, type="StaleTurn", non_retryable=True)
    if message.startswith("AgentBusy: "):
        return ApplicationError(message, type="AgentBusy", non_retryable=True)
    return ApplicationError(message, type="SubagentSendRejected", non_retryable=True)
