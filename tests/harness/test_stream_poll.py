from __future__ import annotations

import pytest
from temporalio.contrib.workflow_streams import WorkflowStreamItem, WorkflowStreamState
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.stream_poll import (
    AgentStreamPollInput,
    AgentStreamPollItem,
    bounded_poll_result,
    replay_stream_state,
)


def _input(from_offset: int) -> AgentStreamPollInput:
    return AgentStreamPollInput(
        from_offset=from_offset,
        topics=["turn_events"],
        timeout_seconds=30,
    )


def test_completed_stream_replay_preserves_poll_cursor_semantics() -> None:
    state = WorkflowStreamState(
        base_offset=3,
        log=[
            WorkflowStreamItem(topic="ignored", data="zero"),
            WorkflowStreamItem(topic="turn_events", data="one"),
            WorkflowStreamItem(topic="turn_events", data="two"),
        ],
    )

    result = replay_stream_state(state, _input(4))

    assert [item.data for item in result.items] == ["one", "two"]
    assert [item.offset for item in result.items] == [4, 5]
    assert result.next_offset == 6
    assert not result.more_ready
    assert result.closed


def test_completed_stream_replay_treats_zero_as_available_history_start() -> None:
    state = WorkflowStreamState(
        base_offset=3,
        log=[WorkflowStreamItem(topic="turn_events", data="one")],
    )

    result = replay_stream_state(state, _input(0))

    assert [item.offset for item in result.items] == [3]
    assert result.next_offset == 4


def test_completed_stream_replay_rejects_a_truncated_specific_cursor() -> None:
    state = WorkflowStreamState(base_offset=3)

    with pytest.raises(ApplicationError, match="has been truncated"):
        replay_stream_state(state, _input(2))


def test_bounded_poll_result_pages_before_temporal_payload_warning() -> None:
    items = [
        AgentStreamPollItem(topic="turn_events", data="a" * 140_000, offset=4),
        AgentStreamPollItem(topic="turn_events", data="b" * 140_000, offset=5),
    ]

    result = bounded_poll_result(
        items,
        next_offset=6,
        more_ready=False,
        closed=False,
    )

    assert result.items == items[:1]
    assert result.next_offset == 5
    assert result.more_ready
    assert not result.closed


def test_bounded_poll_result_advances_past_one_oversized_semantic_event() -> None:
    items = [
        AgentStreamPollItem(topic="turn_events", data="a" * 300_000, offset=4),
        AgentStreamPollItem(topic="turn_events", data="next", offset=5),
    ]

    result = bounded_poll_result(
        items,
        next_offset=6,
        more_ready=False,
        closed=True,
    )

    assert result.items == items[:1]
    assert result.next_offset == 5
    assert result.more_ready
    assert result.closed
