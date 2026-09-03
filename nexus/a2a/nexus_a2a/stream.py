"""Bounded stream paging for the A2A Nexus binding."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from a2a.types import StreamResponse, TaskState

from .errors import NexusA2AError
from .service import SubscribeToTaskItem

TERMINAL_TASK_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
}


@dataclass(frozen=True)
class StreamRecord:
    """One decoded A2A response and its durable stream offset."""

    offset: int
    response: StreamResponse


def decode_stream_item(item: SubscribeToTaskItem) -> StreamResponse:
    """Decode one cursor-addressed Nexus page item into an A2A stream event."""

    response = StreamResponse()
    response.ParseFromString(base64.b64decode(item.data, validate=True))
    if response.WhichOneof("payload") is None:
        raise NexusA2AError("A2A StreamResponse has no payload")
    return response


def decode_stream_record(item: SubscribeToTaskItem) -> StreamRecord:
    """Decode an item without discarding the cursor needed by rich consumers."""

    return StreamRecord(offset=item.offset, response=decode_stream_item(item))


def is_terminal_response(response: StreamResponse) -> bool:
    """Return whether this response ends the current A2A task stream."""

    return response.HasField("status_update") and (
        response.status_update.status.state in TERMINAL_TASK_STATES
    )
