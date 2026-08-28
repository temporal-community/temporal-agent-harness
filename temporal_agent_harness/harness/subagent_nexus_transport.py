# ABOUTME: NexusTransport -- SubagentTransport for a harness agent reached directly over
# Nexus (its own AgentService endpoint, the same contract the Slack connector uses). No
# gateway, no activity: send_agent_message / poll_messages / close_session are plain Nexus
# operations awaited straight from workflow code, mirroring nexus_native_mcp_server.

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

# Each poll_messages call is its own bounded long-poll (waits up to this many seconds for
# new events before returning empty) - the caller just calls again for the next batch.
POLL_TIMEOUT_SECONDS = 25.0


def _decode_stream_item(data: str) -> AgentEvent:
    """StreamItem.data is base64(proto Payload{encoding, TurnEvent JSON}). Decode it the
    same way WorkflowStream's own subscribe() does internally."""
    payload = Payload()
    payload.ParseFromString(base64.b64decode(data))
    return workflow.payload_converter().from_payload(payload, AgentEvent)


class NexusTransport:
    """A harness agent reached over Nexus. `endpoint` is the Nexus endpoint pointing at the
    target's AgentServiceHandler.

    Known limitation: sendAgentMessage's start_config is decided by the target deployment's
    own AgentServiceHandler.Config, not by the caller. So unlike ChildWorkflowTransport, the
    remote subagent's own agent_id is NOT stamped to match the parent's handle - a client
    merging both streams by agent_id will not unify them for this transport.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def _client(self) -> workflow.NexusClient[AgentService]:
        return workflow.create_nexus_client(service=AgentService, endpoint=self._endpoint)

    async def start(self, *, agent_key: str, config: AgentConfig) -> str:
        # Lazy: send_agent_message starts-or-reuses the target workflow on first dispatch.
        return f"{agent_key}-subagent-{workflow.uuid4()}"

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
            sent = await self._client().execute_operation(
                AgentService.send_agent_message,
                SendAgentMessageInput(
                    session_id=target,
                    msg_type=msg_type,
                    payload=json.dumps(payload),
                    expected_turn=expected_turn,
                ),
            )
        except Exception as exc:
            raise _rejection_error(exc) from exc

        # No activity backs this call - it's plain workflow code - so publish the dispatch
        # marker here, right after the send is confirmed, instead of from inside an activity
        # (see ChildWorkflowTransport / subagent_activities.py's _publish_dispatch).
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
                    session_id=target, cursor=cursor, timeout_seconds=POLL_TIMEOUT_SECONDS
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


def _rejection_error(exc: Exception) -> ApplicationError:
    """Turn a send_agent_message rejection into the same ApplicationError type/shape
    ChildWorkflowTransport raises for a pre-acceptance rejection (see handler.py's
    "StaleTurn: "/"AgentBusy: " prefixes) - so AgentWorkflowRunner's error handling doesn't
    need to know which transport is in play."""
    message = str(exc)
    if message.startswith("StaleTurn: "):
        return ApplicationError(message, type="StaleTurn", non_retryable=True)
    if message.startswith("AgentBusy: "):
        return ApplicationError(message, type="AgentBusy", non_retryable=True)
    return ApplicationError(message, type="SubagentSendRejected", non_retryable=True)
