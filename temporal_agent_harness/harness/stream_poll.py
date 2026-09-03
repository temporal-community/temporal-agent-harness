"""Read-only replay helpers for a harness WorkflowStream."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio.contrib.workflow_streams import WorkflowStreamState
from temporalio.exceptions import ApplicationError

from temporal_agent_harness._streaming import (
    AgentStreamPollItem,
    AgentStreamPollResult,
    bounded_poll_result,
)

AGENT_STREAM_REPLAY_QUERY = "__temporal_agent_stream_replay"


@dataclass
class AgentStreamPollInput:
    from_offset: int
    topics: list[str]
    timeout_seconds: float


def replay_stream_state(
    state: WorkflowStreamState,
    input: AgentStreamPollInput,
) -> AgentStreamPollResult:
    """Return one bounded page from a workflow's retained stream state.

    Live consumers normally use the stream poll update because it can wait for new
    data. ``GetTask`` and closed-workflow consumers use this query-compatible helper
    to obtain the same cursor-addressed records from a durable snapshot.
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
