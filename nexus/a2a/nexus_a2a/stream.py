"""Bounded stream paging for the A2A Nexus binding."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from a2a.types import StreamResponse, Task, TaskState
from google.protobuf.json_format import MessageToDict

from .errors import NexusA2AError
from .service import SubscribeToTaskItem

TASK_REPLAY_METADATA_KEY = "temporal.io/nexus-a2a-replay"

TERMINAL_TASK_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
}

CLOSED_TASK_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
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


def task_replay_records(task: Task, *, cursor: int = 0) -> list[StreamRecord]:
    """Decode the replay snapshot carried by ``GetTask``.

    The Nexus binding keeps A2A's unary ``GetTask`` shape and places the ordered,
    cursor-bearing ``StreamResponse`` records in task metadata. Generic A2A clients
    may ignore this extension; replay-capable clients reconstruct the task from it
    before subscribing from :func:`task_replay_cursor`.
    """

    metadata = MessageToDict(task.metadata, preserving_proto_field_name=True)
    replay = metadata.get(TASK_REPLAY_METADATA_KEY)
    if not isinstance(replay, dict):
        return []
    raw_items = replay.get("items")
    if not isinstance(raw_items, list):
        return []
    records: list[StreamRecord] = []
    for value in raw_items:
        if not isinstance(value, dict):
            continue
        try:
            item = SubscribeToTaskItem.model_validate(value)
        except ValueError:
            continue
        if item.offset >= cursor:
            records.append(decode_stream_record(item))
    return records


def task_replay_cursor(task: Task) -> int:
    """Return the live-subscription cursor paired with a ``GetTask`` snapshot."""

    metadata = MessageToDict(task.metadata, preserving_proto_field_name=True)
    replay = metadata.get(TASK_REPLAY_METADATA_KEY)
    if not isinstance(replay, dict):
        return 0
    value = replay.get("next_cursor", 0)
    return int(value) if isinstance(value, (int, float, str)) else 0


def task_is_closed(task: Task) -> bool:
    """Whether an A2A task has reached a closed state."""

    return task.status.state in CLOSED_TASK_STATES


def is_terminal_response(response: StreamResponse) -> bool:
    """Return whether this response ends the current A2A task stream."""

    return response.HasField("status_update") and (
        response.status_update.status.state in TERMINAL_TASK_STATES
    )
