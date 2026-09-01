"""Dependency-neutral stream contracts shared by harness adapters."""

from __future__ import annotations

from dataclasses import dataclass

# The pubsub topic the agent publishes its turn events on. Keep this in a leaf module:
# protocol adapters must be importable without initializing the harness authoring package.
TURN_EVENTS_TOPIC = "turn_events"

# Leave enough headroom for JSON/protobuf framing and the Nexus operation envelope. Temporal
# warns at 512 KiB per payload; returning pages near the workflow-stream SDK's 1 MB default makes
# every page cross that warning boundary more than once on its way through Nexus and the UI tunnel.
_MAX_POLL_RESPONSE_BYTES = 256_000


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


def bounded_poll_result(
    items: list[AgentStreamPollItem],
    *,
    next_offset: int,
    more_ready: bool,
    closed: bool,
) -> AgentStreamPollResult:
    """Page already-encoded stream items below the agent/Nexus payload budget.

    ``WorkflowStream`` currently pages at roughly 1 MB. Agent streams cross two additional
    Temporal boundaries (the Nexus operation and shared UI tunnel), so impose the smaller
    agent-service budget before the workflow update result is serialized. If one semantic event is
    itself larger than the budget, return it alone so the cursor still advances; splitting its
    encoded payload would corrupt the event contract.
    """
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
