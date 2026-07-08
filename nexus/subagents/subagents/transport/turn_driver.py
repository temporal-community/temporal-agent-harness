# ABOUTME: Drives one subagent turn over Nexus (sendMessage/pollTaskUpdates), in workflow code.
# Split into send/poll so the caller learns the turn's id as soon as it's accepted, publishes
# SubagentMessageSent, then polls separately. Sandbox-safe: stdlib+pydantic+SDK types only.

from __future__ import annotations

import base64
import json

from temporalio import workflow
from temporalio.api.common.v1 import Payload as CommonPayload
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import AgentEventType, SubagentTurnResult

from .nexus_agent_service import (
    CancelTaskInput,
    GetTaskInput,
    Message,
    Part,
    PollTaskUpdatesInput,
    SendMessageInput,
    StreamItem,
    SubagentService,
    Task,
)


def _decode_stream_item(item: StreamItem) -> dict:
    """Decode one AgentEvent envelope from base64(proto Payload{...AgentEvent JSON})."""
    raw = base64.b64decode(item.data)
    payload = CommonPayload()
    payload.ParseFromString(raw)
    return json.loads(payload.data)


async def send_message_over_nexus(
    nexus_client: workflow.NexusClient,
    *,
    task_id: str,
    handler: str,
    payload: dict,
) -> Task:
    """Start-or-continue ``task_id`` and send one message to ``handler``; returns once accepted.

    ``task.status.message.message_id`` identifies the turn for the matching
    ``poll_task_updates_over_nexus`` call; ``task.stream_head_offset`` seeds its poll cursor."""
    send_handle = await nexus_client.start_operation(
        SubagentService.send_message,
        SendMessageInput(
            message=Message(
                role="user",
                parts=[Part(kind="data", data=json.dumps({"handler": handler, "input": payload}))],
                task_id=task_id,
            )
        ),
    )
    return await send_handle


async def poll_task_updates_over_nexus(
    nexus_client: workflow.NexusClient,
    *,
    task_id: str,
    message_id: str,
    turn_number: int,
    cursor: int,
) -> SubagentTurnResult:
    """Poll until the turn ``send_message_over_nexus`` started completes, filtered to the exact
    ``message_id``. Raises the same SubagentTurnError/SubagentNoReply shape as the harness's own
    consume activity, so error handling is shared across transports."""
    next_cursor = cursor
    output: dict | None = None
    while True:
        poll_handle = await nexus_client.start_operation(
            SubagentService.poll_task_updates,
            PollTaskUpdatesInput(task_id=task_id, cursor=next_cursor),
        )
        polled = await poll_handle

        for item in polled.items:
            envelope = _decode_stream_item(item)
            if envelope.get("turn_id") != message_id:
                continue
            event = envelope.get("event", {})
            event_type = event.get("type")
            if event_type == AgentEventType.ERROR:
                raise ApplicationError(
                    event.get("message") or "subagent turn failed",
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
                        type="SubagentNoReply",
                        non_retryable=True,
                    )
                return SubagentTurnResult(
                    output=output,
                    turn_id=message_id,
                    turn_number=turn_number,
                    consumed_offset=consumed_offset,
                )

        next_cursor = polled.next_offset
        if polled.closed:
            raise ApplicationError(
                f"Nexus-fronted subagent task {task_id!r} has already closed",
                type="AgentClosed",
                non_retryable=True,
            )


async def get_task_over_nexus(nexus_client: workflow.NexusClient, *, task_id: str) -> Task:
    """Point-in-time task snapshot — no poll cursor consumed."""
    handle = await nexus_client.start_operation(
        SubagentService.get_task, GetTaskInput(task_id=task_id)
    )
    return await handle


async def cancel_task_over_nexus(nexus_client: workflow.NexusClient, *, task_id: str) -> Task:
    """Maps to the harness's generic 'stop' operator command."""
    handle = await nexus_client.start_operation(
        SubagentService.cancel_task, CancelTaskInput(task_id=task_id)
    )
    return await handle
