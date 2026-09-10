"""Project Temporal Agent Harness stream records onto the A2A Nexus binding."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from nexus_a2a import (
    TASK_REPLAY_METADATA_KEY,
    SubscribeToTaskInput,
    SubscribeToTaskItem,
    SubscribeToTaskOutput,
)
from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.contrib.workflow_streams import PollInput

from temporal_agent_harness._streaming import TURN_EVENTS_TOPIC
from temporal_agent_harness.a2a.constants import HARNESS_EVENT_METADATA_KEY


def _timestamp(value: float | None = None) -> Timestamp:
    timestamp = Timestamp()
    timestamp.FromDatetime(
        datetime.fromtimestamp(value, UTC) if value is not None else datetime.now(UTC)
    )
    return timestamp


def harness_envelope(encoded_payload: str) -> dict[str, Any]:
    """Decode one payload produced by the harness WorkflowStream."""

    raw = base64.b64decode(encoded_payload)
    payload = Payload()
    payload.ParseFromString(raw)
    return json.loads(payload.data)


def stream_response(encoded_payload: str) -> StreamResponse:
    """Project one rich harness event into standard A2A plus an extension payload."""

    envelope = harness_envelope(encoded_payload)
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


def task_replay(
    pages: list[SubscribeToTaskOutput], *, task_id: str, history_length: int = 0
) -> tuple[list[Message], list[Artifact], dict[str, Any], int]:
    """Build the standard and lossless replay portions of an A2A ``Task``.

    ``Task.history`` and ``Task.artifacts`` serve ordinary A2A clients. The Nexus
    replay extension preserves every rich harness event and its cursor so the Harness
    UI can reconstruct the exact graph before it starts a live subscription.
    """

    items = [item for page in pages for item in page.items]
    history: list[Message] = []
    artifacts: list[Artifact] = []
    current_turn = 0
    for item in items:
        response = StreamResponse()
        response.ParseFromString(base64.b64decode(item.data))
        if response.HasField("message"):
            message = Message()
            message.CopyFrom(response.message)
            message.task_id = task_id
            message.context_id = task_id
            history.append(message)
        body = response.WhichOneof("payload")
        if body is None:
            continue
        metadata = getattr(response, body).metadata
        values = MessageToDict(metadata, preserving_proto_field_name=True)
        encoded = values.get(HARNESS_EVENT_METADATA_KEY)
        if not isinstance(encoded, str):
            continue
        envelope = harness_envelope(encoded)
        current_turn = max(current_turn, int(envelope.get("turn_number", 0)))
        event = envelope["event"]
        if event["type"] == "turn_started":
            history.append(
                Message(
                    message_id=str(uuid.uuid5(uuid.NAMESPACE_URL, encoded + ":user")),
                    task_id=task_id,
                    context_id=task_id,
                    role=Role.ROLE_USER,
                    parts=[Part(text=str(event.get("user_message", "")))],
                    metadata={HARNESS_EVENT_METADATA_KEY: encoded},
                )
            )
        elif event["type"] == "reply":
            output = event.get("output", {})
            text = (
                output.get("text", json.dumps(output))
                if isinstance(output, dict)
                else str(output)
            )
            artifacts.append(
                Artifact(
                    artifact_id=str(envelope["turn_id"]),
                    parts=[Part(text=text)],
                    metadata={HARNESS_EVENT_METADATA_KEY: encoded},
                )
            )
    if history_length > 0:
        history = history[-history_length:]
    next_cursor = pages[-1].next_cursor if pages else 0
    return (
        history,
        artifacts,
        {
            TASK_REPLAY_METADATA_KEY: {
                "items": [item.model_dump(mode="json") for item in items],
                "next_cursor": next_cursor,
            }
        },
        current_turn,
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


async def poll_subscription_page(
    stream: Any,
    input: SubscribeToTaskInput,
    *,
    is_closed: Callable[[], bool],
) -> SubscribeToTaskOutput:
    """Long-poll a harness stream and project its result onto an A2A page."""

    try:
        await workflow.wait_condition(
            lambda: is_closed() or stream._on_offset() > input.cursor,
            timeout=timedelta(seconds=input.timeout_seconds),
        )
    except TimeoutError:
        return SubscribeToTaskOutput(
            items=[],
            next_cursor=input.cursor,
            closed=is_closed(),
        )
    if is_closed() and stream._on_offset() <= input.cursor:
        return SubscribeToTaskOutput(
            items=[],
            next_cursor=input.cursor,
            closed=True,
        )
    result = await stream._on_poll(
        PollInput(topics=[TURN_EVENTS_TOPIC], from_offset=input.cursor)
    )
    return subscription_page(result, closed=is_closed())
