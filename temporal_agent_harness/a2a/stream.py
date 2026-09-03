"""Project Temporal Agent Harness stream records onto the A2A Nexus binding."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    StreamResponse,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.timestamp_pb2 import Timestamp
from nexus_a2a import SubscribeToTaskItem, SubscribeToTaskOutput
from temporalio.api.common.v1 import Payload

HARNESS_EVENT_METADATA_KEY = "temporal.io/agent-event-payload"
A2A_STREAM_POLL_UPDATE = "__temporal_a2a_subscribe_to_task"
A2A_STREAM_REPLAY_QUERY = "__temporal_a2a_replay_task"


def _timestamp(value: float | None = None) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(
        datetime.fromtimestamp(value, UTC)
        if value is not None
        else datetime.now(UTC)
    )
    return timestamp


def stream_response(encoded_payload: str) -> StreamResponse:
    """Project one rich harness event into standard A2A plus an extension payload."""

    raw = base64.b64decode(encoded_payload)
    payload = Payload()
    payload.ParseFromString(raw)
    envelope = json.loads(payload.data)
    event = envelope["event"]
    event_type = event["type"]
    task_id = str(envelope["agent_id"])
    metadata = {HARNESS_EVENT_METADATA_KEY: encoded_payload}
    if event_type == "reply_delta":
        return StreamResponse(
            artifact_update=TaskArtifactUpdateEvent(
                task_id=task_id,
                context_id=task_id,
                artifact=Artifact(
                    artifact_id=str(envelope["turn_id"]),
                    parts=[Part(text=str(event.get("text", "")))],
                ),
                append=True,
                metadata=metadata,
            )
        )
    if event_type == "reply":
        output = event.get("output", {})
        text = (
            output.get("text", json.dumps(output))
            if isinstance(output, dict)
            else str(output)
        )
        return StreamResponse(
            message=Message(
                message_id=str(uuid.uuid5(uuid.NAMESPACE_URL, encoded_payload)),
                task_id=task_id,
                context_id=task_id,
                role=Role.ROLE_AGENT,
                parts=[Part(text=text)],
                metadata=metadata,
            )
        )
    state = TaskState.TASK_STATE_WORKING
    if event_type == "turn_end":
        state = TaskState.TASK_STATE_INPUT_REQUIRED
    elif event_type == "error":
        state = TaskState.TASK_STATE_FAILED
    return StreamResponse(
        status_update=TaskStatusUpdateEvent(
            task_id=task_id,
            context_id=task_id,
            status=TaskStatus(
                state=state, timestamp=_timestamp(envelope.get("timestamp"))
            ),
            metadata=metadata,
        )
    )

def subscription_page(
    result: Any, *, closed: bool | None = None
) -> SubscribeToTaskOutput:
    """Convert an internal stream-poll result to the Nexus A2A binding page."""

    source_closed = result.closed if closed is None else closed
    return SubscribeToTaskOutput(
        items=[
            SubscribeToTaskItem(
                offset=item.offset,
                data=base64.b64encode(
                    stream_response(item.data).SerializeToString()
                ).decode(),
            )
            for item in result.items
        ],
        next_cursor=result.next_offset,
        more_ready=result.more_ready,
        closed=source_closed and not result.more_ready,
    )
