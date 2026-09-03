"""Dependency-neutral constants shared by harness stream adapters."""

from __future__ import annotations

from dataclasses import dataclass

# The pubsub topic the agent publishes its turn events on. Keep this in a leaf module:
# protocol adapters must be importable without initializing the harness authoring package.
TURN_EVENTS_TOPIC = "turn_events"

# Leave headroom for JSON/protobuf framing and the Nexus operation envelope.
_MAX_POLL_RESPONSE_BYTES = 256_000


@dataclass
class AgentStreamPollItem:
    """Wire-safe copy of one SDK workflow-stream item."""

    topic: str
    data: str
    offset: int = 0


@dataclass
class AgentStreamPollResult:
    items: list[AgentStreamPollItem]
    more_ready: bool
    next_offset: int
    closed: bool


def bounded_poll_result(
    items: list[AgentStreamPollItem],
    *,
    next_offset: int,
    more_ready: bool,
    closed: bool,
) -> AgentStreamPollResult:
    """Page encoded stream items below the agent/Nexus payload budget."""

    page: list[AgentStreamPollItem] = []
    size = 0
    for item in items:
        item_size = len(item.data) + len(item.topic)
        if page and size + item_size > _MAX_POLL_RESPONSE_BYTES:
            return AgentStreamPollResult(
                items=page,
                next_offset=item.offset,
                more_ready=True,
                closed=closed,
            )
        size += item_size
        page.append(item)
    return AgentStreamPollResult(
        items=page,
        next_offset=next_offset,
        more_ready=more_ready,
        closed=closed,
    )
