# ABOUTME: The SSE wire contract for the two offsets a frame carries. ``resume_offset`` is what a
# client hands back to resume; ``event_offset`` with ``agent_id`` is what identifies the event. They
# are different numbers for a reason (a subagent turn's events all share the former), so a frame has
# to carry both and must not conflate them.

from __future__ import annotations

import json

from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
    AgentReply,
    SubagentStreamUnavailable,
)
from temporal_agent_harness.harness.stream_merge import SYNTHESIZED, StreamPosition
from temporal_agent_harness.web.app import _sse, _yield_item


def _frame_data(frame: bytes) -> dict:
    """Parse the ``data:`` payload out of a rendered SSE frame."""
    for line in frame.decode().splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"no data line in frame: {frame!r}")


def _event(agent_id: str, text: str) -> AgentEvent:
    return AgentEvent(
        agent_id=agent_id,
        turn_id="t1",
        turn_number=1,
        timestamp=0.0,
        event=AgentReply(text=text),
    )


def test_frame_carries_both_offsets():
    data = _frame_data(_yield_item(_event("root", "hi"), StreamPosition(12, 11)))
    assert data["resume_offset"] == 12
    assert data["event_offset"] == 11


def test_frame_identifies_subagent_events_that_share_a_resume_offset():
    # The reason event_offset is on the wire at all. Every event of a subagent's turn reports the
    # same resume cursor, so a client keyed on resume_offset alone cannot tell them apart.
    first = _frame_data(_yield_item(_event("child", "a"), StreamPosition(7, 0)))
    second = _frame_data(_yield_item(_event("child", "b"), StreamPosition(7, 1)))
    assert first["resume_offset"] == second["resume_offset"]
    assert (first["agent_id"], first["event_offset"]) != (
        second["agent_id"],
        second["event_offset"],
    ), "agent_id + event_offset must distinguish two events within one subagent turn"


def test_synthesized_marker_reports_no_event_offset():
    marker = AgentEvent(
        agent_id="child",
        turn_id="",
        turn_number=0,
        timestamp=0.0,
        event=SubagentStreamUnavailable(
            subagent_id="child", workflow_id="wf-child", reason="gone"
        ),
    )
    data = _frame_data(_yield_item(marker, StreamPosition(7, SYNTHESIZED)))
    assert data["event_offset"] == SYNTHESIZED


def test_frame_without_a_position_stamps_neither_offset():
    data = _frame_data(_sse("error", {"kind": "timeout", "message": "nope"}))
    assert "resume_offset" not in data
    assert "event_offset" not in data
    assert "replay" not in data


def test_replay_is_on_the_wire_only_when_true():
    # Absence has to mean live, because that is also what an older server and the per-turn chat
    # path mean by never sending it. A client reading `frame.data.replay === true` then needs no
    # version check.
    live = _frame_data(_yield_item(_event("root", "hi"), StreamPosition(12, 11)))
    assert "replay" not in live

    catching_up = _frame_data(
        _yield_item(_event("root", "hi"), StreamPosition(12, 11, replay=True))
    )
    assert catching_up["replay"] is True


def test_replay_defaults_off_so_existing_construction_is_unchanged():
    # Every other caller builds a two-field position; none of them should start claiming replay.
    assert StreamPosition(3, 2).replay is False
