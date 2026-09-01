"""Internal types and helpers for bounded workflow-stream polls."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio.contrib.workflow_streams import WorkflowStreamState
from temporalio.exceptions import ApplicationError

AGENT_STREAM_POLL_UPDATE = "__temporal_agent_stream_poll"
AGENT_STREAM_REPLAY_QUERY = "__temporal_agent_stream_replay"
# Leave enough headroom for JSON/protobuf framing and the Nexus operation envelope. Temporal
# warns at 512 KiB per payload; returning pages near the workflow-stream SDK's 1 MB default makes
# every page cross that warning boundary more than once on its way through Nexus and the UI tunnel.
_MAX_POLL_RESPONSE_BYTES = 256_000


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

    items = [
        AgentStreamPollItem(topic=item.topic, data=item.data, offset=offset)
        for offset, item in candidates
    ]
    return bounded_poll_result(
        items,
        next_offset=state.base_offset + len(state.log),
        more_ready=False,
        closed=True,
    )
