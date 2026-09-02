# ABOUTME: The seam between "catching you up" and "showing you what's happening now". attach stamps
# replay on every event that was already durable when the stream opened, which a consumer uses to
# batch its commits during history and stop batching once it is following. Getting this wrong in the
# permissive direction (over-claiming replay) is survivable; the other way flashes the canvas.

from __future__ import annotations

from unittest.mock import patch

import pytest

import temporal_agent_harness.harness.agent_client as agent_client_mod
import temporal_agent_harness.harness.stream_merge.cursor as cursor_mod
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentEvent, AgentReply
from temporal_agent_harness.harness.stream_merge import StreamPosition
from tests.harness.test_stream_merge import (
    _FakeStreams,
    _ms,
    _reply,
    _rr,
    _te,
    _tool,
    _ts,
)

pytestmark = pytest.mark.asyncio


def _event(agent_id: str, offset: int) -> AgentEvent:
    return AgentEvent(
        agent_id=agent_id,
        turn_id="t1",
        turn_number=1,
        timestamp=0.0,
        event=AgentReply(text=f"e{offset}"),
    )


async def _stamps(scripted: list[StreamPosition], *, head: int) -> list[bool]:
    """Drive _merged_attach over scripted positions and collect the replay flag it stamps."""

    async def fake_merge(**_kwargs):
        for position in scripted:
            yield (_event("root", position.event_offset), position)

    client = AgentClient(temporal=None, workflow_id="wf-root")
    with patch.object(agent_client_mod, "merge_stream", fake_merge):
        stream = client._merged_attach(
            on_item=lambda _item, position: position.replay,
            from_offset=0,
            stop_at_root_offset=head,
            stall_grace_seconds=1.0,
        )
        return [flag async for flag in stream]


async def test_events_already_durable_are_replay_and_later_ones_are_not():
    # The seam is the head as of attach: at or below it the event was already in the log.
    scripted = [StreamPosition(resume_offset=n, event_offset=n - 1) for n in range(1, 7)]
    assert await _stamps(scripted, head=3) == [True, True, True, False, False, False]


async def test_nothing_is_replay_when_the_stream_opened_empty():
    # head 0 means the log was empty, so every event arriving is live. This is the case that a
    # stamp hardcoded to True would pass and this asserts against.
    scripted = [StreamPosition(resume_offset=n, event_offset=n - 1) for n in range(1, 4)]
    assert await _stamps(scripted, head=0) == [False, False, False]


async def test_everything_is_replay_when_attaching_to_a_finished_run():
    scripted = [StreamPosition(resume_offset=n, event_offset=n - 1) for n in range(1, 4)]
    assert await _stamps(scripted, head=99) == [True, True, True]


async def test_a_subagents_events_inherit_its_dispatch_position():
    # A child's resume_offset stands still for its turn, so every event of a turn dispatched at or
    # before the seam is stamped replay even if the child published it live. Deliberate: it errs
    # toward "still catching up", and a consumer must survive a long catch-up anyway.
    scripted = [
        StreamPosition(resume_offset=3, event_offset=0),
        StreamPosition(resume_offset=3, event_offset=1),
        StreamPosition(resume_offset=3, event_offset=2),
    ]
    assert await _stamps(scripted, head=3) == [True, True, True]


async def test_the_stamp_never_flips_back_to_replay_once_the_seam_is_crossed():
    """One attach cannot interleave replay and live: it flips at most once, and never back.

    Why, structurally rather than by example. ``_root_resume_offset`` is seeded once at root
    mount and thereafter only ever assigned ``ev_offset + 1`` from a root stream read in
    increasing offset order, and every yield snapshots its current value -- so the
    ``resume_offset`` sequence over a merged stream is monotonically non-decreasing. ``replay``
    is ``resume_offset <= stop_at_root_offset`` with a fixed seam: a monotone threshold on a
    non-decreasing sequence, hence a run of True then a run of False.

    A child's ``resume_offset`` is therefore NOT frozen at its dispatch, which is the premise
    this test exists to refute. It is the cursor as of the last root event already emitted, so a
    root event that crosses the seam before the child publishes carries the child across too.
    """
    # Driven over the REAL merge (test_stream_merge's fake streams), on the shape that would
    # interleave the flag if a subagent's resume_offset really froze at its dispatch: C is
    # dispatched AT the seam, and the root then crosses the seam (offset 2) BEFORE C publishes
    # anything. The merge re-reads the root cursor at every emit rather than freezing it, so C's
    # live events carry the crossed value and the flag stays a single True-run then a False-run.
    streams = {
        "P": [
            _ts("P", 1),  # 0
            _ms("P", 1, child="C", child_turn=1, from_offset=0),  # 1 — dispatch at the seam
            _tool("P", 1, "t", start=True),  # 2 — root crosses the seam here
            _rr("P", 1, child="C", child_turn=1),  # 3 — close-gated, so C fills the bracket
            _reply("P", 1),  # 4
            _te("P", 1),  # 5
        ],
        "C": [_ts("C", 1), _reply("C", 1), _te("C", 1)],
    }
    client = AgentClient(temporal=None, workflow_id="P")
    with patch.object(cursor_mod, "WorkflowStreamClient", _FakeStreams(streams)):
        stream = client._merged_attach(
            on_item=lambda _ev, position: position,
            from_offset=0,
            stop_at_root_offset=2,
            stall_grace_seconds=1.0,
        )
        positions = [position async for position in stream]
    # ts, message_sent | tool_start, C's whole turn, reply_received, reply, turn_end. C's three
    # events carry 3 — the cursor as of the root event that crossed the seam, not the 2 it stood at
    # when C was dispatched — which is what keeps their stamp from flipping back to replay.
    assert [p.resume_offset for p in positions] == [1, 2, 3, 3, 3, 3, 4, 5, 6]
    flags = [p.replay for p in positions]
    assert flags == [True, True] + [False] * 7
    transitions = sum(a != b for a, b in zip(flags, flags[1:], strict=False))
    assert transitions == 1, f"replay flag interleaved: {flags}"
    # This holds PER ATTACH and is not a promise to a consumer that merges several. Each attach
    # gets its own seam, so a subagent attached separately (the console does this) replays its
    # backlog while the root's frames are live, and the marks interleave in whatever pipeline
    # joins them. See catchingUpAfterFrame() in ui/src/lib/state/hydration.ts, which latches at
    # the first live frame rather than trusting the mark per frame.


# The topologies that could plausibly break the ordering: concurrent siblings whose brackets
# overlap, a recursive grandchild, and a child that fills one bracket with many events while the
# root's cursor stands still. Reusing test_stream_merge's own shapes rather than inventing any.
_TOPOLOGIES = {
    "concurrent siblings": (
        "P",
        {
            "P": [
                _ts("P", 1),
                _ms("P", 1, child="C", child_turn=1),
                _ms("P", 1, child="D", child_turn=1),
                _rr("P", 1, child="C", child_turn=1),
                _rr("P", 1, child="D", child_turn=1),
                _reply("P", 1),
                _te("P", 1),
            ],
            "C": [_ts("C", 1), _reply("C", 1), _te("C", 1)],
            "D": [_ts("D", 1), _reply("D", 1), _te("D", 1)],
        },
    ),
    "recursive grandchild": (
        "P",
        {
            "P": [
                _ts("P", 1),
                _ms("P", 1, child="C", child_turn=1),
                _rr("P", 1, child="C", child_turn=1),
                _te("P", 1),
            ],
            "C": [
                _ts("C", 1),
                _ms("C", 1, child="G", child_turn=1),
                _rr("C", 1, child="G", child_turn=1),
                _reply("C", 1),
                _te("C", 1),
            ],
            "G": [_ts("G", 1), _reply("G", 1), _te("G", 1)],
        },
    ),
    "long child bracket": (
        "P",
        {
            "P": [
                _ts("P", 1),
                _ms("P", 1, child="C", child_turn=1),
                _rr("P", 1, child="C", child_turn=1),
                _reply("P", 1),
                _te("P", 1),
            ],
            "C": [_ts("C", 1), *[_reply("C", 1) for _ in range(8)], _te("C", 1)],
        },
    ),
}


@pytest.mark.parametrize("topology", list(_TOPOLOGIES))
async def test_resume_offset_only_advances_so_no_seam_can_interleave_the_stamp(topology):
    """The monotonicity above, at EVERY possible seam rather than one chosen one.

    Sweeping the head is what makes it a property and not an anecdote: if any topology could
    hand a consumer a replay frame after a live one, some seam value would expose it.
    """
    root, streams = _TOPOLOGIES[topology]
    seams_exercised = 0
    for head in range(-1, sum(len(events) for events in streams.values()) + 2):
        client = AgentClient(temporal=None, workflow_id=root)
        with patch.object(cursor_mod, "WorkflowStreamClient", _FakeStreams(streams)):
            stream = client._merged_attach(
                on_item=lambda _ev, position: position,
                from_offset=0,
                stop_at_root_offset=head,
                stall_grace_seconds=1.0,
            )
            positions = [position async for position in stream]
        assert positions, f"{topology} at head {head}: the merge yielded nothing to assert on"
        offsets = [p.resume_offset for p in positions]
        assert offsets == sorted(offsets), f"{topology} at head {head}: resume_offset went backwards"
        flags = [p.replay for p in positions]
        transitions = sum(a != b for a, b in zip(flags, flags[1:], strict=False))
        assert transitions <= 1, f"{topology} at head {head}: replay interleaved as {flags}"
        assert flags == sorted(flags, reverse=True), f"{topology} at head {head}: {flags}"
        seams_exercised += transitions
    # A sweep that never straddles a seam would pass on an all-live stream and prove nothing.
    assert seams_exercised >= 2, f"{topology}: swept no real catch-up-to-live seam"
