"""Internal types and helpers for bounded workflow-stream polls."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio.contrib.workflow_streams import WorkflowStreamState
from temporalio.exceptions import ApplicationError

AGENT_STREAM_POLL_UPDATE = "__temporal_agent_stream_poll"
AGENT_STREAM_REPLAY_QUERY = "__temporal_agent_stream_replay"
_MAX_POLL_RESPONSE_BYTES = 1_000_000


@dataclass
class AgentStreamPollInput:
    from_offset: int
    topics: list[str]
    timeout_seconds: float


@dataclass
class AgentStreamPollItem:
    """Wire-safe copy of an SDK workflow-stream item.

    ``WorkflowStreamItem`` is generic, which Temporal's JSON converter cannot rebuild
    when a completed workflow query is decoded outside the workflow worker. Keep the
    internal SDK type behind this transport boundary so live polls and replay queries
    share a concrete, serializable result shape.
    """

    topic: str
    data: str
    offset: int = 0


@dataclass
class AgentStreamPollResult:
    items: list[AgentStreamPollItem]
    more_ready: bool
    next_offset: int
    closed: bool


def replay_stream_state(
    state: WorkflowStreamState,
    input: AgentStreamPollInput,
) -> AgentStreamPollResult:
    """Return one bounded page from a completed workflow's stream snapshot.

    Live consumers use the stream poll update because it can wait for new data. Once a
    workflow has completed, updates are rejected but queries can still replay the workflow
    and inspect its final stream state. Keep this page calculation aligned with
    ``WorkflowStream._on_poll`` so a Nexus caller can use the same cursor before and after
    completion.
    """
    log_offset = input.from_offset - state.base_offset
    if log_offset < 0:
        if input.from_offset == 0:
            log_offset = 0
        else:
            raise ApplicationError(
                f"Requested offset {input.from_offset} has been truncated. "
                f"Current base offset is {state.base_offset}.",
                type="TruncatedOffset",
            )

    all_new = state.log[log_offset:]
    topic_set = set(input.topics)
    candidates = [
        (state.base_offset + log_offset + index, item)
        for index, item in enumerate(all_new)
        if not topic_set or item.topic in topic_set
    ]

    items: list[AgentStreamPollItem] = []
    size = 0
    more_ready = False
    next_offset = state.base_offset + len(state.log)
    for offset, item in candidates:
        item_size = len(item.data) + len(item.topic)
        if size + item_size > _MAX_POLL_RESPONSE_BYTES and items:
            next_offset = offset
            more_ready = True
            break
        size += item_size
        items.append(
            AgentStreamPollItem(topic=item.topic, data=item.data, offset=offset)
        )

    return AgentStreamPollResult(
        items=items,
        more_ready=more_ready,
        next_offset=next_offset,
        closed=True,
    )
