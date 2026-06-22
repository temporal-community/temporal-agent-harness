# ABOUTME: Tests for the client-side stream-merge (harness/stream_merge/) — the layer that
# coalesces a parent agent's stream + its (recursive, possibly concurrent) subagent streams into
# one logical stream. Two layers of tests:
#   * pure gate logic (gates.py) — ready()/on_emit() against the bracket invariants, no I/O;
#   * the merge engine (merge.py) — driven over scripted in-memory streams via a fake stream
#     client, asserting EVERY merged ordering is semantically valid (within-stream order + both
#     happens-before brackets), that replay is deterministic, and that recursion/concurrency work.
#
# The validity checker is the heart of these tests: rather than pin one exact interleaving (the
# design deliberately allows cross-stream reordering), it asserts the merged stream is always a
# *possible* ordering — which is the actual contract.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from temporalio.contrib.workflow_streams import WorkflowStreamItem

import temporal_agent_harness.harness.stream_merge.cursor as cursor_mod
from temporal_agent_harness.harness.agent_protocol import (
    TURN_EVENTS_TOPIC,
    AgentEvent,
    AgentEventType,
    AgentReply,
    SubagentMessageSent,
    SubagentReplyReceived,
    SubagentStarted,
    SubagentStopped,
    ToolEndEvent,
    ToolStartEvent,
    TurnEnded,
    TurnStarted,
)
from temporal_agent_harness.harness.stream_merge import (
    Gates,
    MountChild,
    UnmountChild,
    merge_stream,
    select_live,
    select_replay,
)

# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _ev(agent_id: str, turn_number: int, payload: Any, *, turn_id: str | None = None) -> AgentEvent:
    return AgentEvent(
        agent_id=agent_id,
        turn_id=turn_id or f"{agent_id}-t{turn_number}",
        turn_number=turn_number,
        timestamp=0.0,
        event=payload,
    )


def _ts(agent_id: str, turn: int) -> AgentEvent:
    return _ev(agent_id, turn, TurnStarted(user_message="hi"))


def _reply(agent_id: str, turn: int) -> AgentEvent:
    return _ev(agent_id, turn, AgentReply(output={"ok": True}))


def _te(agent_id: str, turn: int) -> AgentEvent:
    return _ev(agent_id, turn, TurnEnded())


def _ms(agent_id: str, parent_turn: int, *, child: str, child_turn: int, from_offset: int = 0) -> AgentEvent:
    return _ev(
        agent_id,
        parent_turn,
        SubagentMessageSent(
            subagent_id=child[:6],
            agent_key="k",
            workflow_id=child,
            function="f",
            subagent_turn=child_turn,
            from_offset=from_offset,
        ),
    )


def _rr(agent_id: str, parent_turn: int, *, child: str, child_turn: int, outcome: str = "ok") -> AgentEvent:
    return _ev(
        agent_id,
        parent_turn,
        SubagentReplyReceived(
            subagent_id=child[:6],
            agent_key="k",
            workflow_id=child,
            function="f",
            subagent_turn=child_turn,
            outcome=outcome,  # type: ignore[arg-type]
        ),
    )


def _started(agent_id: str, parent_turn: int, *, child: str) -> AgentEvent:
    return _ev(
        agent_id,
        parent_turn,
        SubagentStarted(subagent_id=child[:6], agent_key="k", workflow_id=child),
    )


def _stopped(agent_id: str, parent_turn: int, *, child: str) -> AgentEvent:
    return _ev(
        agent_id,
        parent_turn,
        SubagentStopped(subagent_id=child[:6], agent_key="k", workflow_id=child),
    )


def _tool(agent_id: str, turn: int, tid: str, *, start: bool) -> AgentEvent:
    payload = (
        ToolStartEvent(tool_id=tid, tool_name="x", tool_input={})
        if start
        else ToolEndEvent(tool_id=tid, tool_name="x", tool_output="done")
    )
    return _ev(agent_id, turn, payload)


# ---------------------------------------------------------------------------
# Fake stream client — scripted in-memory streams, patched into cursor.py
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(
        self,
        events: list[AgentEvent],
        *,
        closes: list[str] | None = None,
        workflow_id: str = "",
        fail_after: int | None = None,
        live_tail: bool = False,
        drip: float | None = None,
    ) -> None:
        self._events = events
        self._closes = closes
        self._workflow_id = workflow_id
        # When set, the (fail_after+1)-th item raises a read error (mimics the per-workflow
        # concurrent-update cap / a poll against a completed child) — to test graceful skip.
        self._fail_after = fail_after
        # When True, the generator BLOCKS after exhausting its backlog instead of stopping — mimics
        # subscribe() live-tailing a still-running workflow (its poll update stays in flight). Used
        # to prove unmount-on-subagent_stopped actually closes that held subscription.
        self._live_tail = live_tail
        # When set, sleep this many seconds BEFORE each yielded item — a stream that delivers in a
        # slow real-time drip. Used to prove the PER-CHILD stall deadline: a steadily-dripping
        # sibling (each delivery < stall_grace apart) must NOT keep resetting a dead child's clock.
        self._drip = drip

    def subscribe(
        self,
        topics: Any = None,
        from_offset: int = 0,
        *,
        result_type: Any = None,
        poll_cooldown: Any = None,
    ) -> AsyncIterator[WorkflowStreamItem[AgentEvent]]:
        events = self._events
        fail_after = self._fail_after
        live_tail = self._live_tail
        closes = self._closes
        workflow_id = self._workflow_id
        drip = self._drip

        async def gen() -> AsyncIterator[WorkflowStreamItem[AgentEvent]]:
            # Finite backlog then StopAsyncIteration — unlike the live server stream, which would
            # block tailing. Finite streams make the merge's ordering deterministically testable.
            # The cursor closes this generator (its `aclose`) on unmount/teardown; record that via
            # GeneratorExit so a test can assert the merge released the subscription.
            try:
                for n, offset in enumerate(range(from_offset, len(events))):
                    if fail_after is not None and n >= fail_after:
                        raise RuntimeError("simulated stream read error (e.g. update cap)")
                    if drip is not None:
                        await asyncio.sleep(drip)
                    yield WorkflowStreamItem(
                        topic=TURN_EVENTS_TOPIC, data=events[offset], offset=offset
                    )
                if live_tail:
                    # Block forever (until cancelled by aclose) — a live, idle workflow's poll.
                    await asyncio.Event().wait()
            except (GeneratorExit, asyncio.CancelledError):
                if closes is not None:
                    closes.append(workflow_id)
                raise

        return gen()


class _FakeStreams:
    def __init__(
        self,
        streams: dict[str, list[AgentEvent]],
        *,
        closes: list[str] | None = None,
        fail_workflows: dict[str, int] | None = None,
        live_tail_workflows: set[str] | None = None,
        drip_workflows: dict[str, float] | None = None,
    ) -> None:
        self._streams = streams
        self._closes = closes
        self._fail_workflows = fail_workflows or {}
        self._live_tail_workflows = live_tail_workflows or set()
        self._drip_workflows = drip_workflows or {}

    def create(self, _client: Any, workflow_id: str) -> _FakeHandle:
        return _FakeHandle(
            self._streams.get(workflow_id, []),
            closes=self._closes,
            workflow_id=workflow_id,
            fail_after=self._fail_workflows.get(workflow_id),
            live_tail=workflow_id in self._live_tail_workflows,
            drip=self._drip_workflows.get(workflow_id),
        )


async def _never_stop(_cursor: Any, _ev: AgentEvent) -> bool:
    return False


async def _run_merge(
    streams: dict[str, list[AgentEvent]],
    *,
    root: str,
    select: Any,
    skip_until_turn_id: str | None = None,
    root_from_offset: int = 0,
    should_stop: Any = None,
    closes: list[str] | None = None,
    fail_workflows: dict[str, int] | None = None,
    live_tail_workflows: set[str] | None = None,
    drip_workflows: dict[str, float] | None = None,
    stall_grace_seconds: float = 5.0,
    resume_offsets: list[int] | None = None,
) -> list[AgentEvent]:
    """Drive the merge over scripted streams to exhaustion (or should_stop), returning the output.

    ``closes`` (if given) collects the workflow_ids whose subscription the merge closed (unmount /
    teardown). ``fail_workflows`` maps a workflow_id to the item count after which its stream raises
    a read error. ``live_tail_workflows`` makes the named streams block after their backlog (a live
    idle workflow) so unmount-on-stop — and the stall backstop on a never-delivering child — can be
    exercised without the backlog naturally ending. ``stall_grace_seconds`` is the liveness backstop
    (tests pass a tiny value so a simulated hang resolves fast). ``root_from_offset`` (with no
    ``skip_until_turn_id``) exercises the no-skip resume path. ``resume_offsets`` (if given) collects
    the per-event root resume offset the merge yields."""
    fake = _FakeStreams(
        streams,
        closes=closes,
        fail_workflows=fail_workflows,
        live_tail_workflows=live_tail_workflows,
        drip_workflows=drip_workflows,
    )
    out: list[AgentEvent] = []
    with patch.object(cursor_mod, "WorkflowStreamClient", fake):
        async for ev, resume_offset in merge_stream(
            client=None,
            root_workflow_id=root,
            root_from_offset=root_from_offset,
            skip_until_turn_id=skip_until_turn_id,
            select=select,
            should_stop=should_stop or _never_stop,
            stall_grace_seconds=stall_grace_seconds,
        ):
            out.append(ev)
            if resume_offsets is not None:
                resume_offsets.append(resume_offset)
    return out


# ---------------------------------------------------------------------------
# Validity checker — the contract: a *possible* ordering, not one exact order
# ---------------------------------------------------------------------------


def _pos(merged: list[AgentEvent], ev: AgentEvent) -> int:
    for i, m in enumerate(merged):
        if m is ev:
            return i
    raise AssertionError("event not found in merged output")


def assert_valid_merge(merged: list[AgentEvent], streams: dict[str, list[AgentEvent]]) -> None:
    """Assert ``merged`` is a semantically-valid coalescing of ``streams``.

    1. **Completeness + within-stream order:** the subsequence of ``merged`` from each agent
       equals that agent's input stream exactly (same events, same order, no drops, no dupes).
    2. **Open + close brackets:** for every ``subagent_reply_received(C, T)`` there is an earlier
       ``subagent_message_sent(C, T)``, and ALL of child C's turn-T events fall strictly between
       the two markers.
    """
    # (1) within-stream order + completeness
    for wf, events in streams.items():
        got = [m for m in merged if m.agent_id == wf]
        assert got == events, f"stream {wf}: within-stream order/completeness violated"

    # (2) brackets, keyed off each reply_received marker
    for i, m in enumerate(merged):
        if not isinstance(m.event, SubagentReplyReceived):
            continue
        child, t = m.event.workflow_id, m.event.subagent_turn
        opens = [
            j
            for j, x in enumerate(merged)
            if isinstance(x.event, SubagentMessageSent)
            and x.event.workflow_id == child
            and x.event.subagent_turn == t
        ]
        assert opens, f"reply_received({child},{t}) has no matching message_sent"
        j = opens[0]
        assert j < i, f"message_sent({child},{t}) must precede its reply_received"
        # every child-C turn-T event sits strictly inside (j, i)
        for k, x in enumerate(merged):
            if x.agent_id == child and x.turn_number == t:
                assert j < k < i, (
                    f"child {child} turn {t} event escaped its bracket "
                    f"(open={j}, event={k}, close={i})"
                )


# ---------------------------------------------------------------------------
# Pure gate tests (gates.py)
# ---------------------------------------------------------------------------


def test_open_gate_holds_child_until_message_sent_emitted():
    gates = Gates()
    child_ts = _ts("C", 1)
    # Child event is held until the parent's message_sent for that turn has been emitted.
    assert not gates.ready(is_child=True, source_workflow_id="C", ev=child_ts)
    mount = gates.on_emit(
        is_child=False, source_workflow_id="P", ev=_ms("P", 1, child="C", child_turn=1, from_offset=7)
    )
    # _ms stamps subagent_id = child[:6] ("C"); the mount carries it so a later give-up can label
    # the child even if it delivered no events of its own.
    assert mount == MountChild(workflow_id="C", from_offset=7, subagent_id="C")
    assert gates.ready(is_child=True, source_workflow_id="C", ev=child_ts)


def test_close_gate_holds_reply_received_until_child_turn_end_emitted():
    gates = Gates()
    gates.on_emit(is_child=False, source_workflow_id="P", ev=_ms("P", 1, child="C", child_turn=1))
    rr = _rr("P", 1, child="C", child_turn=1)
    # reply_received waits for child C's turn_end to have been emitted.
    assert not gates.ready(is_child=False, source_workflow_id="P", ev=rr)
    gates.on_emit(is_child=True, source_workflow_id="C", ev=_te("C", 1))
    assert gates.ready(is_child=False, source_workflow_id="P", ev=rr)


def test_root_events_are_never_open_gated():
    gates = Gates()
    # A non-child (root) event is ready regardless of opened-set state.
    assert gates.ready(is_child=False, source_workflow_id="P", ev=_ts("P", 1))


def test_on_emit_returns_mount_only_for_message_sent():
    gates = Gates()
    assert gates.on_emit(is_child=False, source_workflow_id="P", ev=_ts("P", 1)) is None
    assert gates.on_emit(is_child=True, source_workflow_id="C", ev=_te("C", 1)) is None
    mount = gates.on_emit(
        is_child=False, source_workflow_id="P", ev=_ms("P", 1, child="C", child_turn=1)
    )
    assert isinstance(mount, MountChild)


# ---------------------------------------------------------------------------
# Engine tests (merge.py) over scripted streams
# ---------------------------------------------------------------------------


def _parent_one_child_turn() -> dict[str, list[AgentEvent]]:
    """Parent drives child C for one turn; child does a tool call inside."""
    return {
        "P": [
            _ts("P", 1),
            _ms("P", 1, child="C", child_turn=1),
            _rr("P", 1, child="C", child_turn=1),
            _reply("P", 1),
            _te("P", 1),
        ],
        "C": [
            _ts("C", 1),
            _tool("C", 1, "c1", start=True),
            _tool("C", 1, "c1", start=False),
            _reply("C", 1),
            _te("C", 1),
        ],
    }


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_single_subagent_turn_is_nested_in_its_bracket(select):
    streams = _parent_one_child_turn()
    merged = await _run_merge(streams, root="P", select=select)
    assert_valid_merge(merged, streams)
    # All input events present.
    assert len(merged) == len(streams["P"]) + len(streams["C"])


async def test_replay_is_deterministic():
    streams = _parent_one_child_turn()
    a = await _run_merge(streams, root="P", select=select_replay)
    b = await _run_merge(streams, root="P", select=select_replay)
    assert a == b
    # And it's the canonical fully-nested order (root drained until gated, child fills the bracket).
    kinds = [(m.agent_id, m.event.type) for m in a]
    assert kinds == [
        ("P", AgentEventType.TURN_STARTED),
        ("P", AgentEventType.SUBAGENT_MESSAGE_SENT),
        ("C", AgentEventType.TURN_STARTED),
        ("C", AgentEventType.TOOL_START),
        ("C", AgentEventType.TOOL_END),
        ("C", AgentEventType.REPLY),
        ("C", AgentEventType.TURN_END),
        ("P", AgentEventType.SUBAGENT_REPLY_RECEIVED),
        ("P", AgentEventType.REPLY),
        ("P", AgentEventType.TURN_END),
    ]


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_reused_subagent_two_turns_one_parent_turn(select):
    # A gathered/sequential pair of sends to the SAME child in one parent turn. The FIFO gate makes
    # the brackets non-overlapping: ms(C,1) rr(C,1) ms(C,2) rr(C,2). The child's turn-2 events must
    # wait for ms(C,2) even though its cursor is already mounted (open gate for the second turn).
    streams = {
        "P": [
            _ts("P", 1),
            _ms("P", 1, child="C", child_turn=1, from_offset=0),
            _rr("P", 1, child="C", child_turn=1),
            _ms("P", 1, child="C", child_turn=2, from_offset=3),
            _rr("P", 1, child="C", child_turn=2),
            _te("P", 1),
        ],
        "C": [
            _ts("C", 1),
            _reply("C", 1),
            _te("C", 1),
            _ts("C", 2),
            _reply("C", 2),
            _te("C", 2),
        ],
    }
    merged = await _run_merge(streams, root="P", select=select)
    assert_valid_merge(merged, streams)


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_two_concurrent_subagents_overlapping_brackets(select):
    # Different subagents may have OVERLAPPING brackets (parent drives C and D in one turn). Each
    # child's turn must still be fully nested in ITS own bracket; the two may interleave freely.
    streams = {
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
    }
    merged = await _run_merge(streams, root="P", select=select)
    assert_valid_merge(merged, streams)


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_nested_grandchild_recursion(select):
    # P drives C; within C's turn, C drives grandchild G. Brackets nest: G's turn sits inside C's
    # turn, which sits inside P's bracket for C. The merge mounts G off C's own message_sent.
    streams = {
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
    }
    merged = await _run_merge(streams, root="P", select=select)
    assert_valid_merge(merged, streams)


async def test_send_message_skip_preamble_starts_at_target_turn_started():
    # A resume mid-session: the root stream has a prior turn (turn 1) we must NOT emit, then our
    # target turn 2. We start at accepted_offset and skip until turn 2's turn_started.
    target = "P-t2"
    streams = {
        "P": [
            _ts("P", 1),  # offset 0 — prior turn, must be skipped
            _reply("P", 1),  # offset 1 — skipped
            _te("P", 1),  # offset 2 — skipped
            _ev("P", 2, TurnStarted(user_message="go"), turn_id=target),  # offset 3
            _ev("P", 2, AgentReply(output={}), turn_id=target),
            _ev("P", 2, TurnEnded(), turn_id=target),
        ],
    }

    async def stop_at_target_turn_end(cursor: Any, ev: AgentEvent) -> bool:
        return (
            not cursor.is_child
            and ev.turn_id == target
            and ev.event.type == AgentEventType.TURN_END
        )

    merged = await _run_merge(
        streams,
        root="P",
        select=select_live,
        skip_until_turn_id=target,
        root_from_offset=3,
        should_stop=stop_at_target_turn_end,
    )
    # Only the target turn's events, starting at its turn_started.
    assert [m.event.type for m in merged] == [
        AgentEventType.TURN_STARTED,
        AgentEventType.REPLY,
        AgentEventType.TURN_END,
    ]
    assert all(m.turn_id == target for m in merged)


async def test_send_message_resume_mounts_reused_child_at_from_offset():
    # Resume on parent turn 2, which drives child C's turn 2. C's turns 1 (from an earlier, skipped
    # parent turn) must NOT appear — the merge mounts C at the from_offset carried on message_sent,
    # skipping C's pre-resume history (whose own message_sent isn't on this merged stream).
    target = "P-t2"
    streams = {
        "P": [
            _ts("P", 1),
            _ms("P", 1, child="C", child_turn=1, from_offset=0),
            _rr("P", 1, child="C", child_turn=1),
            _te("P", 1),
            _ev("P", 2, TurnStarted(user_message="go"), turn_id=target),
            _ms("P", 2, child="C", child_turn=2, from_offset=3),  # C turn 2 begins at child offset 3
            _rr("P", 2, child="C", child_turn=2),
            _ev("P", 2, TurnEnded(), turn_id=target),
        ],
        "C": [
            _ts("C", 1),  # offset 0 — belongs to parent turn 1; must NOT be merged on resume
            _reply("C", 1),  # offset 1
            _te("C", 1),  # offset 2
            _ts("C", 2),  # offset 3 — where turn 2 begins (from_offset=3)
            _reply("C", 2),  # offset 4
            _te("C", 2),  # offset 5
        ],
    }

    async def stop_at_target_turn_end(cursor: Any, ev: AgentEvent) -> bool:
        return (
            not cursor.is_child
            and ev.turn_id == target
            and ev.event.type == AgentEventType.TURN_END
        )

    merged = await _run_merge(
        streams,
        root="P",
        select=select_replay,
        skip_until_turn_id=target,
        root_from_offset=4,
        should_stop=stop_at_target_turn_end,
    )
    # No turn-1 events from either stream; child turn-2 events nested in their bracket.
    assert all(m.turn_number == 2 for m in merged)
    child_turns = {m.turn_number for m in merged if m.agent_id == "C"}
    assert child_turns == {2}
    # bracket validity for the C-turn-2 sub-slice
    open_i = next(
        i for i, m in enumerate(merged) if isinstance(m.event, SubagentMessageSent)
    )
    close_i = next(
        i for i, m in enumerate(merged) if isinstance(m.event, SubagentReplyReceived)
    )
    child_positions = [i for i, m in enumerate(merged) if m.agent_id == "C"]
    assert all(open_i < i < close_i for i in child_positions)


async def test_stop_predicate_ends_the_merge_at_root_turn_end():
    # With a should_stop on the root's turn_end, the merge ends right there even though the fake
    # streams could yield more — and by the close gate the whole child subtree is already drained.
    streams = _parent_one_child_turn()

    async def stop(cursor: Any, ev: AgentEvent) -> bool:
        return not cursor.is_child and ev.event.type == AgentEventType.TURN_END

    merged = await _run_merge(streams, root="P", select=select_replay, should_stop=stop)
    assert merged[-1].agent_id == "P"
    assert merged[-1].event.type == AgentEventType.TURN_END
    assert_valid_merge(merged, streams)


async def test_empty_root_stream_yields_nothing():
    merged = await _run_merge({"P": []}, root="P", select=select_replay)
    assert merged == []


# ---------------------------------------------------------------------------
# Unmount-on-subagent_stopped + graceful degradation (Bug 1 regressions)
# ---------------------------------------------------------------------------


def test_on_emit_returns_unmount_for_subagent_stopped():
    # The pure gate signals an unmount when a subagent_stopped is emitted (symmetric to the mount
    # on subagent_message_sent), so the engine can release that child's in-flight poll update.
    gates = Gates()
    action = gates.on_emit(
        is_child=False, source_workflow_id="P", ev=_stopped("P", 1, child="C")
    )
    assert action == UnmountChild(workflow_id="C")


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_child_cursor_unmounted_on_subagent_stopped(select):
    # The child's turn runs and ends inside its bracket, THEN the parent stops the subagent. The
    # child stream live-tails (its poll would stay in flight forever), so the merge can only finish
    # — and the test can only complete — if subagent_stopped actually closes the child's cursor.
    streams = {
        "P": [
            _ts("P", 1),
            _started("P", 1, child="C"),
            _ms("P", 1, child="C", child_turn=1),
            _rr("P", 1, child="C", child_turn=1),
            _stopped("P", 1, child="C"),
            _te("P", 1),
        ],
        "C": [_ts("C", 1), _reply("C", 1), _te("C", 1)],
    }
    closes: list[str] = []
    merged = await _run_merge(
        streams,
        root="P",
        select=select,
        closes=closes,
        live_tail_workflows={"C"},  # C never ends on its own — only an unmount closes it
    )
    assert_valid_merge(merged, streams)
    # The child's subscription was closed when subagent_stopped was emitted (not only at teardown).
    assert "C" in closes


async def test_unreadable_child_does_not_crash_the_merge():
    # The child stream raises a read error (e.g. the per-workflow concurrent-update cap, or a poll
    # against a completed/stopped child) right at its first event. The merge must DEGRADE
    # GRACEFULLY: drop that child, RELEASE its close gate so the parent flows to its end, and
    # surface a non-fatal subagent_stream_unavailable marker — never crash or wedge.
    streams = {
        "P": [
            _ts("P", 1),
            _ms("P", 1, child="C", child_turn=1),
            _rr("P", 1, child="C", child_turn=1),  # close-gated on C's turn_end, which never comes
            _reply("P", 1),
            _te("P", 1),
        ],
        "C": [_ts("C", 1), _reply("C", 1), _te("C", 1)],
    }
    merged = await _run_merge(
        streams,
        root="P",
        select=select_replay,
        fail_workflows={"C": 0},  # C's very first read raises
    )
    # The ENTIRE root stream flows — including reply_received, reply, turn_end that sit AFTER the
    # close gate. The dead child must not strand the parent's own events.
    assert [m.event.type for m in merged if m.agent_id == "P"] == [
        AgentEventType.TURN_STARTED,
        AgentEventType.SUBAGENT_MESSAGE_SENT,
        AgentEventType.SUBAGENT_REPLY_RECEIVED,
        AgentEventType.REPLY,
        AgentEventType.TURN_END,
    ]
    # No actual child turn DETAIL leaked (no C turn_started/reply/turn_end)...
    child_detail = [
        m for m in merged if m.agent_id == "C" and m.event.type != AgentEventType.SUBAGENT_STREAM_UNAVAILABLE
    ]
    assert not child_detail
    # ...but exactly one non-fatal unavailable marker was surfaced for C (agent_id == its id).
    markers = [m for m in merged if m.event.type == AgentEventType.SUBAGENT_STREAM_UNAVAILABLE]
    assert len(markers) == 1
    assert markers[0].agent_id == "C" and markers[0].event.subagent_id == "C"
    assert markers[0].event.workflow_id == "C"


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_dead_child_releases_close_gate_and_parent_completes(select):
    # The user's exact repro: on replay the subagent was stopped, so its stream never delivers
    # turn_end. The child cursor HANGS (live_tail = blocks forever), the classic deadlock: the
    # parent's reply_received is close-gated on a turn_end that can't come. The stall backstop must
    # give up on the child so the parent's reply_received + reply + turn_end still flow.
    streams = {
        "P": [
            _ts("P", 1),
            _started("P", 1, child="C"),
            _ms("P", 1, child="C", child_turn=1),
            _rr("P", 1, child="C", child_turn=1),
            _reply("P", 1),
            _te("P", 1),
        ],
        "C": [],  # never delivers anything; live_tail makes its pull block forever (a hang)
    }
    merged = await _run_merge(
        streams,
        root="P",
        select=select,
        live_tail_workflows={"C"},
        stall_grace_seconds=0.05,  # tiny so the simulated hang resolves fast in the test
    )
    assert [m.event.type for m in merged if m.agent_id == "P"] == [
        AgentEventType.TURN_STARTED,
        AgentEventType.SUBAGENT_STARTED,
        AgentEventType.SUBAGENT_MESSAGE_SENT,
        AgentEventType.SUBAGENT_REPLY_RECEIVED,
        AgentEventType.REPLY,
        AgentEventType.TURN_END,
    ]
    markers = [m for m in merged if m.event.type == AgentEventType.SUBAGENT_STREAM_UNAVAILABLE]
    assert len(markers) == 1 and markers[0].event.subagent_id == "C"


async def test_dead_child_given_up_on_its_own_deadline_despite_chatty_sibling():
    # PER-CHILD stall deadline: C is dead and close-gates the parent's reply_received(C);
    # sibling D streams a long turn in a slow real-time DRIP (each delivery < stall_grace apart). A
    # single shared wait-timeout would be re-armed by every D delivery, deferring C's give-up until D
    # finally went quiet — so C's marker would land AFTER D's turn_end. With a per-child deadline, C is
    # given up ~stall_grace after IT first blocked, regardless of D's chatter — so C's marker lands
    # BEFORE D's turn_end. We assert exactly that ordering (clock-free to check: an index comparison).
    d_turn = (
        [_ts("D", 1)]
        + [_tool("D", 1, f"d{i}", start=True) for i in range(18)]  # 18 dripped progress events
        + [_reply("D", 1), _te("D", 1)]
    )
    streams = {
        "P": [
            _ts("P", 1),
            _ms("P", 1, child="C", child_turn=1),   # C dispatched first → rr(C) precedes rr(D)
            _ms("P", 1, child="D", child_turn=1),
            _rr("P", 1, child="C", child_turn=1),    # gated on C (dead) — blocks the root cursor
            _rr("P", 1, child="D", child_turn=1),    # behind rr(C) in parent order
            _reply("P", 1),
            _te("P", 1),
        ],
        "C": [],                                     # dead: live_tail blocks forever
        "D": d_turn,
    }
    merged = await _run_merge(
        streams,
        root="P",
        select=select_replay,
        live_tail_workflows={"C"},
        drip_workflows={"D": 0.02},   # D delivers every 20ms (< the 100ms grace)
        stall_grace_seconds=0.1,
    )
    # D's full turn detail is preserved (the chatty sibling is never given up).
    assert [m.event.type for m in merged if m.agent_id == "D"][-1] == AgentEventType.TURN_END
    # Exactly one unavailable marker, for the dead child C.
    markers = [i for i, m in enumerate(merged) if m.event.type == AgentEventType.SUBAGENT_STREAM_UNAVAILABLE]
    assert len(markers) == 1 and merged[markers[0]].event.subagent_id == "C"
    # The dead child was given up on ITS OWN deadline: its marker lands BEFORE D's turn_end — not
    # deferred until D's drip went quiet (which the old shared-timer behavior would have done).
    d_turn_end_idx = next(
        i for i, m in enumerate(merged) if m.agent_id == "D" and m.event.type == AgentEventType.TURN_END
    )
    assert markers[0] < d_turn_end_idx, (
        "dead child C was not given up until sibling D went quiet — per-child deadline regressed"
    )


async def test_child_that_ends_without_turn_end_releases_gate():
    # Variant where the child stream cleanly ENDS (StopAsyncIteration) mid-turn instead of hanging
    # — e.g. a completed workflow whose backlog stops before turn_end. Same outcome: the close gate
    # releases (no stall wait needed, since the cursor exhausts), the parent completes, marker fires.
    streams = {
        "P": [
            _ts("P", 1),
            _ms("P", 1, child="C", child_turn=1),
            _rr("P", 1, child="C", child_turn=1),
            _te("P", 1),
        ],
        "C": [_ts("C", 1), _reply("C", 1)],  # ends before turn_end
    }
    merged = await _run_merge(streams, root="P", select=select_replay)
    assert [m.event.type for m in merged if m.agent_id == "P"] == [
        AgentEventType.TURN_STARTED,
        AgentEventType.SUBAGENT_MESSAGE_SENT,
        AgentEventType.SUBAGENT_REPLY_RECEIVED,
        AgentEventType.TURN_END,
    ]
    # C's available detail (turn_started, reply) still came through before it ran out.
    assert [m.event.type for m in merged if m.agent_id == "C" and m.event.type != AgentEventType.SUBAGENT_STREAM_UNAVAILABLE] == [
        AgentEventType.TURN_STARTED,
        AgentEventType.REPLY,
    ]
    assert sum(m.event.type == AgentEventType.SUBAGENT_STREAM_UNAVAILABLE for m in merged) == 1


async def test_healthy_concurrent_child_is_not_given_up_under_stall_grace():
    # Guard against the stall backstop misfiring: a healthy child that delivers its turn (readable)
    # must NOT be given up on, even with a tiny grace — its turn_end arrives, the gate closes
    # normally, and NO unavailable marker is emitted.
    streams = _parent_one_child_turn()
    merged = await _run_merge(
        streams, root="P", select=select_replay, stall_grace_seconds=0.05
    )
    assert_valid_merge(merged, streams)
    assert not [m for m in merged if m.event.type == AgentEventType.SUBAGENT_STREAM_UNAVAILABLE]


async def test_unreadable_root_ends_merge_without_raising():
    # If the ROOT stream itself becomes unreadable mid-replay, the merge ends gracefully (no
    # exception out of the generator — which would 500 the BFF) after the events read so far.
    streams = {
        "P": [_ts("P", 1), _reply("P", 1), _te("P", 1)],
    }
    merged = await _run_merge(
        streams, root="P", select=select_replay, fail_workflows={"P": 1}
    )
    # Got the events before the failure, then a clean end (no raise).
    assert [m.event.type for m in merged] == [AgentEventType.TURN_STARTED]


# ---------------------------------------------------------------------------
# attach(from_offset) — root-offset resume (the cursor advances on every root event)
# ---------------------------------------------------------------------------


def _three_turn_root() -> dict[str, list[AgentEvent]]:
    """A root with three back-to-back turns (no subagents), offsets 0..8."""
    return {
        "P": [
            _ts("P", 1), _reply("P", 1), _te("P", 1),   # offsets 0,1,2
            _ts("P", 2), _reply("P", 2), _te("P", 2),   # offsets 3,4,5
            _ts("P", 3), _reply("P", 3), _te("P", 3),   # offsets 6,7,8
        ],
    }


async def test_resume_offset_advances_past_each_root_event():
    # The resume cursor advances past EVERY root event emitted (here the root has no subagents, so
    # every event is a root event and the cursor advances on each — any offset is a safe resume point
    # under the no-skip policy). A subagent's own events would instead repeat the prior root value.
    streams = _three_turn_root()
    offsets: list[int] = []
    merged = await _run_merge(
        streams, root="P", select=select_replay, resume_offsets=offsets
    )
    assert len(merged) == 9
    # Each root event at offset i hands back resume offset i+1.
    assert offsets == [1, 2, 3, 4, 5, 6, 7, 8, 9]


async def test_resume_from_offset_streams_only_events_after_it():
    # Resuming from offset 3 (start of turn 2) yields turns 2 and 3 — turn 1 is not re-sent. No
    # skip: the root simply starts at offset 3, which is turn 2's turn_started.
    streams = _three_turn_root()
    merged = await _run_merge(
        streams, root="P", select=select_replay, root_from_offset=3
    )
    assert {e.turn_number for e in merged} == {2, 3}
    assert merged[0].event.type == AgentEventType.TURN_STARTED and merged[0].turn_number == 2


async def test_resume_mid_turn_streams_the_rest_of_that_turn_no_fast_forward():
    # The NON-AGGRESSIVE policy: a mid-turn resume offset (offset 4 = turn 2's reply) streams the
    # REST of turn 2 (reply, turn_end) and then turn 3 — it does NOT fast-forward past turn 2.
    streams = _three_turn_root()
    merged = await _run_merge(
        streams, root="P", select=select_replay, root_from_offset=4
    )
    assert [e.event.type for e in merged] == [
        AgentEventType.REPLY, AgentEventType.TURN_END,        # rest of turn 2 (from offset 4)
        AgentEventType.TURN_STARTED, AgentEventType.REPLY, AgentEventType.TURN_END,  # turn 3
    ]
    assert [e.turn_number for e in merged] == [2, 2, 3, 3, 3]


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_resume_includes_subagent_dispatched_at_or_after_offset(select):
    # Resume at the start of parent turn 2 (offset 4), which drives subagent C (turn 2). C's turn-2
    # message_sent is at/after the offset, so C mounts at its from_offset (3) and turn-2 detail
    # merges normally; C's turn-1 detail (dispatched before the offset) is not re-sent.
    streams = {
        "P": [
            _ts("P", 1),                                              # 0
            _ms("P", 1, child="C", child_turn=1, from_offset=0),     # 1
            _rr("P", 1, child="C", child_turn=1),                    # 2
            _te("P", 1),                                             # 3
            _ts("P", 2),                                             # 4  (resume here)
            _ms("P", 2, child="C", child_turn=2, from_offset=3),     # 5
            _rr("P", 2, child="C", child_turn=2),                    # 6
            _te("P", 2),                                             # 7
        ],
        "C": [
            _ts("C", 1), _reply("C", 1), _te("C", 1),                # 0,1,2  (turn 1 — not re-sent)
            _ts("C", 2), _reply("C", 2), _te("C", 2),                # 3,4,5  (turn 2)
        ],
    }
    merged = await _run_merge(streams, root="P", select=select, root_from_offset=4)
    assert all(e.turn_number == 2 for e in merged)
    assert {e.turn_number for e in merged if e.agent_id == "C"} == {2}
    open_i = next(i for i, m in enumerate(merged) if m.event.type == AgentEventType.SUBAGENT_MESSAGE_SENT)
    close_i = next(i for i, m in enumerate(merged) if m.event.type == AgentEventType.SUBAGENT_REPLY_RECEIVED)
    child_pos = [i for i, m in enumerate(merged) if m.agent_id == "C"]
    assert child_pos and all(open_i < i < close_i for i in child_pos)


@pytest.mark.parametrize("select", [select_replay, select_live])
async def test_resume_inside_subagent_turn_omits_that_subagent_but_parent_flows(select):
    # THE user's invariant: resuming at an offset INSIDE subagent C's turn (after its message_sent,
    # before its reply_received) means C was never mounted (we didn't see its turn start), so C's
    # events are absent — but the parent's reply_received(C) is RELEASED (unmounted-stuck give-up)
    # so the parent flows. No fast-forward, no marker (we never showed C's detail to "lose").
    streams = {
        "P": [
            _ts("P", 1),                                              # 0
            _ms("P", 1, child="C", child_turn=1, from_offset=0),     # 1
            _tool("P", 1, "t", start=True),                          # 2  (resume here — mid C's turn)
            _rr("P", 1, child="C", child_turn=1),                    # 3
            _reply("P", 1),                                          # 4
            _te("P", 1),                                             # 5
        ],
        "C": [_ts("C", 1), _reply("C", 1), _te("C", 1)],
    }
    merged = await _run_merge(streams, root="P", select=select, root_from_offset=2)
    # Parent's whole tail from offset 2 flows; NO C events; NO unavailable marker.
    assert [e.event.type for e in merged if e.agent_id == "P"] == [
        AgentEventType.TOOL_START,
        AgentEventType.SUBAGENT_REPLY_RECEIVED,
        AgentEventType.REPLY,
        AgentEventType.TURN_END,
    ]
    assert not [m for m in merged if m.agent_id == "C"]
    assert not [m for m in merged if m.event.type == AgentEventType.SUBAGENT_STREAM_UNAVAILABLE]


async def test_redispatched_given_up_child_reenables_its_close_gate():
    # If a child is given up on (resumed past its turn-1 start → unmounted-stuck release) and then
    # RE-DISPATCHED in a later turn, the fresh turn's close gate must hold again (gone is cleared on
    # re-mount) — i.e. its turn-2 detail is properly nested, not bypassed.
    streams = {
        "P": [
            _ts("P", 1),                                              # 0
            _ms("P", 1, child="C", child_turn=1, from_offset=0),     # 1
            _rr("P", 1, child="C", child_turn=1),                    # 2 (resume here — C never mounted)
            _ms("P", 1, child="C", child_turn=2, from_offset=3),     # 3 (re-dispatch C)
            _rr("P", 1, child="C", child_turn=2),                    # 4
            _te("P", 1),                                             # 5
        ],
        "C": [
            _ts("C", 1), _reply("C", 1), _te("C", 1),                # 0,1,2 (turn 1 — never mounted)
            _ts("C", 2), _reply("C", 2), _te("C", 2),                # 3,4,5 (turn 2 — mounts at 3)
        ],
    }
    merged = await _run_merge(streams, root="P", select=select_replay, root_from_offset=2)
    # Turn-1 reply_received released (C unmounted), then C turn-2 mounts and its detail nests in the
    # turn-2 bracket — proving the close gate works again after the give-up.
    assert {e.turn_number for e in merged if e.agent_id == "C"} == {2}
    # Find the SECOND message_sent / reply_received (turn 2) and assert C's events sit between them.
    ms_positions = [i for i, m in enumerate(merged) if m.event.type == AgentEventType.SUBAGENT_MESSAGE_SENT]
    rr_positions = [i for i, m in enumerate(merged) if m.event.type == AgentEventType.SUBAGENT_REPLY_RECEIVED]
    open_i, close_i = ms_positions[-1], rr_positions[-1]
    child_pos = [i for i, m in enumerate(merged) if m.agent_id == "C"]
    assert child_pos and all(open_i < i < close_i for i in child_pos)
