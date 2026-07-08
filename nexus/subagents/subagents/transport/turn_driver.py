# ABOUTME: Drives one subagent turn against a Nexus-fronted agent (nexus/agent_adapter) to
# completion — the Nexus-transport sibling of the harness's own child-workflow-plus-activity
# turn driver (temporal_agent_harness.harness.subagent_activities.SubagentActivities
# .run_subagent_turn). Unlike that activity, there is no server-side chaining and no new Nexus
# operation: it just loops the EXISTING ``sendAgentMessage``/``pollMessages`` operations from
# workflow code (the same pattern the Slack connector's driver already uses), each individual
# poll still going through update-with-callback underneath. This module is called from workflow
# code (via NexusTransport.send_turn), so it must stay sandbox-safe: only stdlib + pydantic +
# the SDK's own types (``temporalio.workflow``, the protobuf ``Payload`` message) — no client
# machinery.

from __future__ import annotations

import base64
import json
from collections.abc import Callable

from temporalio import workflow
from temporalio.api.common.v1 import Payload as CommonPayload
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import AgentEventType, SubagentTurnResult

from .nexus_agent_service import (
    AgentService,
    PollMessagesInput,
    SendAgentMessageInput,
    SendMessageOutput,
    StreamItem,
)


def _decode_stream_item(item: StreamItem) -> dict:
    """One ``AgentEvent`` envelope, as the plain dict the harness published — ``item.data`` is
    base64(proto Payload{encoding:json/plain, data:AgentEvent JSON}), exactly what
    ``WorkflowStream._log`` stores (see ``StreamItem``'s docstring)."""
    raw = base64.b64decode(item.data)
    payload = CommonPayload()
    payload.ParseFromString(raw)
    return json.loads(payload.data)


async def run_subagent_turn_over_nexus(
    nexus_client: workflow.NexusClient,
    *,
    session_id: str,
    msg_type: str,
    payload: dict,
    cursor: int,
    on_sent: Callable[[SendMessageOutput], None] | None = None,
) -> SubagentTurnResult:
    """Send one message to a Nexus-fronted agent and drive its turn to completion.

    Sends once via ``sendAgentMessage``, then loops ``pollMessages`` — filtered to the EXACT
    ``turn_id`` the send returned (not the Slack connector driver's looser ``turn_number >=``
    check) and with an explicit ``turn_end``-with-no-reply backstop (the connector's driver has
    neither, which is fine for a chat UI but not for something threading turn-exact bookkeeping
    back into a FIFO gate) — until that turn's ``turn_end``. Raises the SAME
    ``SubagentTurnError``/``SubagentNoReply`` ``ApplicationError`` shape (``{"subagent_turn":
    ...}`` in ``details``) the harness's own child-workflow transport raises, so
    ``AgentWorkflowRunner.run_subagent_turn``'s error handling (bracket-closing, turn-counter
    advance) is shared across both transports unchanged.

    ``on_sent`` fires right after the send succeeds, before polling starts — the caller uses it
    to publish ``SubagentMessageSent`` at the moment the message was actually sent (mirroring
    where the child-workflow transport publishes it), not after the whole turn completes.
    """
    send_handle = await nexus_client.start_operation(
        AgentService.send_agent_message,
        SendAgentMessageInput(sessionId=session_id, msgType=msg_type, payload=json.dumps(payload)),
    )
    sent = await send_handle
    if on_sent is not None:
        on_sent(sent)

    turn_number = sent.turn_number
    next_cursor = cursor if cursor else sent.stream_head_offset
    output: dict | None = None
    while True:
        poll_handle = await nexus_client.start_operation(
            AgentService.poll_messages,
            PollMessagesInput(sessionId=session_id, cursor=next_cursor),
        )
        polled = await poll_handle

        for item in polled.items:
            envelope = _decode_stream_item(item)
            if envelope.get("turn_id") != sent.turn_id:
                continue
            event = envelope.get("event", {})
            event_type = event.get("type")
            if event_type == AgentEventType.ERROR:
                raise ApplicationError(
                    event.get("message") or "subagent turn failed",
                    {"subagent_turn": turn_number},
                    type="SubagentTurnError",
                    non_retryable=True,
                )
            if event_type == AgentEventType.REPLY:
                output = event.get("output", {})
            if event_type == AgentEventType.TURN_END:
                consumed_offset = item.offset + 1
                if output is None:
                    raise ApplicationError(
                        f"subagent turn {turn_number} ended without a reply",
                        {"subagent_turn": turn_number},
                        type="SubagentNoReply",
                        non_retryable=True,
                    )
                return SubagentTurnResult(
                    output=output,
                    turn_id=sent.turn_id,
                    turn_number=turn_number,
                    consumed_offset=consumed_offset,
                )

        next_cursor = polled.next_offset
        if polled.closed:
            raise ApplicationError(
                f"Nexus-fronted subagent session {session_id!r} has already closed",
                type="AgentClosed",
                non_retryable=True,
            )
