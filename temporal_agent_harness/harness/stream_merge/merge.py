# ABOUTME: The gated k-way merge engine — coalesces a root agent's stream and, recursively, every
# subagent stream it mounts into ONE logical stream that always respects (a) each stream's own
# offset order and (b) the two happens-before brackets in gates.py. Ordering never consults
# timestamps (Temporal clocks across machines are uncoordinated). The ONLY thing that differs
# between a live view and a later replay is how genuinely-concurrent (un-bracketed) events from
# different streams interleave — selected by an injectable ``select`` policy.

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from temporalio.client import Client

from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
    SubagentReplyReceived,
    SubagentStreamUnavailable,
)
from temporal_agent_harness.harness.stream_merge.cursor import Cursor
from temporal_agent_harness.harness.stream_merge.gates import (
    Gates,
    MountChild,
    UnmountChild,
)

_log = logging.getLogger(__name__)

# Liveness backstop (seconds): how long the merge will wait on an in-flight child pull that is the
# ONLY thing blocking a buffered parent ``subagent_reply_received`` before presuming that child
# unreachable and giving up on it. This is NOT an ordering input (ordering stays gate/offset-driven,
# clock-free) — it's purely "stop waiting for a subagent that will never answer." It only ever
# applies while a parent reply is close-gated on a child (see ``_Merge._drive``); an idle/slow LIVE
# stream that is NOT close-gate-blocked is waited on indefinitely, so this never spuriously fires
# there. In replay a buffered ``reply_received`` proves the child turn already completed, so a
# readable child delivers well within this window and only a dead/unreachable one trips it.
DEFAULT_STALL_GRACE_SECONDS = 5.0

# A select policy ranks the cursors whose heads are READY this step and returns the one to emit.
# Both policies below are total over a non-empty candidate list and use no clock.
SelectPolicy = Callable[[list[Cursor]], Cursor]

# Called after each emitted event; returns True to end the merged stream. The two entry points
# (a single turn vs. a full attach) differ only in this and in the select policy + start offset.
ShouldStop = Callable[[Cursor, AgentEvent], Awaitable[bool]]

# What the merge yields per step: the event plus the **resume offset** — a ROOT-stream resume CURSOR
# (NOT a per-event coordinate) that a consumer records and hands back to ``attach(from_offset=...)``
# to resume without re-replaying already-seen root events. It advances past EVERY root event emitted
# (to ``ev_offset + 1``) and does NOT move on a subagent's own events — so every event within one
# subagent turn's bracket carries the SAME value: the cursor as of that turn's
# ``subagent_message_sent`` (the root offset, not the child's own offset). Any root offset is a safe
# resume point (see ``merge_stream``); it is neither the event's own per-stream offset nor a merged
# display ordinal (the cross-stream interleaving itself is never a resumable position).
MergedItem = tuple[AgentEvent, int]


def select_replay(candidates: list[Cursor]) -> Cursor:
    """Replay policy: lowest ``mount_index`` first (root, then children in mount order).

    Purely structural, so re-deriving a fixed backlog yields the SAME interleaving every time —
    no clock, fully reproducible. It never stalls on an idle child: only cursors that already hold
    a ready head are candidates, so a quiet stream simply isn't considered."""
    return min(candidates, key=lambda c: c.mount_index)


def select_live(candidates: list[Cursor]) -> Cursor:
    """Live policy: emit ready heads in the order they ARRIVED (lowest ``head_seq``).

    Gives a real-time feel while tailing — whichever stream produced first goes first — and, like
    every policy, only ranks already-ready candidates so an idle stream can never block the rest."""
    return min(candidates, key=lambda c: c.head_seq)


class _Merge:
    """One merge run. Owns the cursor set, the bracket :class:`Gates`, and the drive loop."""

    def __init__(
        self,
        *,
        client: Client,
        select: SelectPolicy,
        should_stop: ShouldStop,
        stall_grace_seconds: float = DEFAULT_STALL_GRACE_SECONDS,
    ) -> None:
        self._client = client
        self._select = select
        self._should_stop = should_stop
        self._stall_grace = stall_grace_seconds
        self._gates = Gates()
        self._cursors: dict[str, Cursor] = {}
        self._mount_seq = 0
        self._arrival_seq = 0
        # The root cursor's workflow id (set at mount). A read error on the ROOT is terminal for
        # the merge — there is no other stream the consumer is here for; a read error on a CHILD is
        # survivable (drop it, keep coalescing the rest).
        self._root_workflow_id: str | None = None
        # workflow_id -> the child's short subagent_id (from the mounting subagent_message_sent), so
        # a give-up can label its synthesized ``subagent_stream_unavailable`` marker even if the child
        # delivered no events of its own to carry that id.
        self._subagent_ids: dict[str, str] = {}
        # Children we've given up on but not yet emitted a marker for — drained by ``_drive`` into
        # the merged stream as synthetic ``subagent_stream_unavailable`` events.
        self._pending_markers: list[AgentEvent] = []
        # The ROOT-stream resume cursor handed back to the consumer (see ``MergedItem``). Seeded to
        # the offset the merge started from (resuming there again loses nothing) and advanced past
        # each ROOT event as it is emitted (subagent events leave it unchanged).
        self._root_resume_offset = 0
        # PER-CHILD stall deadlines: child_workflow_id -> the event-loop time by which, if that
        # child is STILL the (sole) thing close-gating a buffered parent ``subagent_reply_received``,
        # we presume it unreachable and give up on it. Set when a child first starts blocking and
        # kept until it stops blocking (or is given up) — so a chatty SIBLING delivering events
        # cannot keep resetting a dead child's clock (which a single shared ``asyncio.wait`` timeout
        # would do). This is a liveness timer, NOT an ordering input (ordering stays gate/offset-
        # driven, clock-free); it only ever governs how long we wait on a close-gate-blocking child.
        self._stall_deadlines: dict[str, float] = {}

    def _mount(
        self,
        workflow_id: str,
        *,
        from_offset: int,
        is_child: bool,
        skip_until_turn_id: str | None = None,
    ) -> None:
        """Mount a stream as a new cursor (idempotent — a re-used child is mounted once).

        Idempotency is load-bearing: a parent may drive the same subagent across many turns, each
        re-emitting ``subagent_message_sent`` for that child — but the child's cursor is mounted on
        the FIRST and keeps advancing sequentially; later turns' ``from_offset`` just equal where
        the cursor already sits. The ``skip_until_turn_id`` preamble is only ever meaningful for the
        ROOT cursor (send_message)."""
        if workflow_id in self._cursors:
            return
        if is_child:
            # If we'd previously given up on this child (so it's in ``gone``, its close gates
            # released) and the parent now RE-DISPATCHES it, this fresh turn's close gate must hold
            # again — clear the stale ``gone`` flag. (A child is only both gone and re-mountable
            # after a give-up unmounted it; the re-mount means it's reachable for this new turn.)
            self._gates.gone.discard(workflow_id)
        else:
            self._root_workflow_id = workflow_id
            # Resuming there again would lose nothing — seed the resume offset to the start point.
            self._root_resume_offset = from_offset
        self._cursors[workflow_id] = Cursor.mount(
            self._client,
            workflow_id=workflow_id,
            is_child=is_child,
            mount_index=self._mount_seq,
            from_offset=from_offset,
            skip_until_turn_id=skip_until_turn_id,
        )
        self._mount_seq += 1

    async def _unmount(self, workflow_id: str) -> None:
        """Close a child's cursor and drop it from the active set (idempotent).

        Releases the in-flight long-poll *update* that cursor holds against ``workflow_id`` — the
        thing that, accumulated across mounts that never unmounted, eventually hit the per-workflow
        concurrent-update cap. Called on a ``subagent_stopped`` (see :class:`UnmountChild`) and when
        a child cursor turns out to be unreadable/exhausted. Never drops the ROOT cursor: the merge
        ends through ``should_stop`` / root exhaustion, not by unmounting the stream the consumer is
        watching."""
        cur = self._cursors.get(workflow_id)
        if cur is None or not cur.is_child:
            return
        del self._cursors[workflow_id]
        await cur.aclose()

    async def run(
        self,
        *,
        root_workflow_id: str,
        root_from_offset: int,
        skip_until_turn_id: str | None,
    ) -> AsyncIterator[MergedItem]:
        self._mount(
            root_workflow_id,
            from_offset=root_from_offset,
            is_child=False,
            skip_until_turn_id=skip_until_turn_id,
        )
        try:
            async for item in self._drive():
                yield item
        finally:
            await self._teardown()

    async def _drive(self) -> AsyncIterator[MergedItem]:
        while True:
            # Drain any synthetic ``subagent_stream_unavailable`` markers queued by a give-up first, so a
            # consumer learns a subagent's detail was dropped right where it happened.
            while self._pending_markers:
                yield (self._pending_markers.pop(0), self._root_resume_offset)
            self._ensure_pulls()
            candidates = [
                c
                for c in self._cursors.values()
                if c.head is not None
                and self._gates.ready(
                    is_child=c.is_child, source_workflow_id=c.workflow_id, ev=c.head
                )
            ]
            if candidates:
                cur = self._select(candidates)
                ev = cur.head
                assert ev is not None  # candidates filter guarantees it
                ev_offset = cur.head_offset
                cur.head = None  # consumed → _ensure_pulls re-pulls this cursor next loop
                # Advance the resume cursor PAST every ROOT event emitted (any root offset is a safe
                # resume point — ``attach`` resumes with NO skip, and a subagent whose turn began
                # before the resume offset is simply never mounted, its ``reply_received`` released by
                # the unmounted-stuck give-up). Subagent events leave the cursor unchanged, so they
                # all carry the value as of their triggering ``subagent_message_sent`` — which means
                # a reconnect mid a subagent turn forgoes that subagent's remaining detail (lossless
                # only at root-event granularity; see ``merge_stream``).
                if not cur.is_child:
                    self._root_resume_offset = ev_offset + 1
                yield (ev, self._root_resume_offset)
                action = self._gates.on_emit(
                    is_child=cur.is_child, source_workflow_id=cur.workflow_id, ev=ev
                )
                if isinstance(action, MountChild):
                    self._subagent_ids[action.workflow_id] = action.subagent_id
                    self._mount(
                        action.workflow_id, from_offset=action.from_offset, is_child=True
                    )
                elif isinstance(action, UnmountChild):
                    # The child is drained + idle by the time its subagent_stopped surfaces, so
                    # closing its cursor strands no gated event and frees its in-flight poll update.
                    # mark_gone too (harmless — its turns are all ended — but keeps the rule "a
                    # departed child never close-gates the parent" uniform).
                    self._gates.mark_gone(action.workflow_id)
                    await self._unmount(action.workflow_id)
                if await self._should_stop(cur, ev):
                    return
                continue

            # No ready head. The ONLY way a stalled/dead child can wedge the parent is a buffered
            # parent ``subagent_reply_received`` close-gated on that child; that's the only thing we
            # ever bound a wait on. ``_sync_stall_deadlines`` returns the currently close-gate-
            # blocking children and arms a PER-CHILD deadline for each new one (clearing any that
            # stopped blocking) — per-child so a chatty sibling can't reset a dead child's clock.
            stuck_children = self._sync_stall_deadlines()
            now = self._now()

            # Give up on a stuck child we cannot keep waiting on:
            #   * UNMOUNTED — its ``subagent_message_sent`` was never emitted (an ``attach`` that
            #     resumed PAST that subagent's turn start), so its ``turn_end`` can never arrive;
            #     release at once, no wait (its detail is simply absent from this resumed view).
            #   * deadline ELAPSED — presumed unreachable after its own grace window.
            # Either way, releasing the close gate lets the parent's reply (and everything after it)
            # flow. ``gone`` membership makes ``_give_up`` idempotent across both trigger paths.
            give_up_now = [
                wf
                for wf in stuck_children
                if wf not in self._cursors or self._stall_deadlines[wf] <= now
            ]
            if give_up_now:
                for workflow_id in give_up_now:
                    await self._give_up(workflow_id)
                continue

            pending = [
                c.pull_task
                for c in self._cursors.values()
                if c.pull_task is not None and not c.pull_task.done()
            ]
            if pending:
                # Wake on the next delivered event OR the EARLIEST child stall deadline (so a child
                # is given up its own grace after ITS OWN last delivery while blocking — not after
                # global quiet, which a chatty sibling could defer forever). No close-gate-blocking
                # child ⇒ no timeout: a genuinely idle/slow LIVE stream is waited on indefinitely and
                # never spuriously dropped.
                timeout = (
                    min(self._stall_deadlines.values()) - now
                    if self._stall_deadlines
                    else None
                )
                done, _ = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if done and await self._collect_completed_pulls():
                    # The ROOT became unreadable (per-workflow update cap, workflow gone). There is
                    # no stream left — end gracefully rather than raise out of the generator (which
                    # would 500 the BFF mid-frame). The consumer can re-attach.
                    return
                # Whether a pull delivered (re-check candidates) or the deadline elapsed (the
                # give-up branch fires next iteration), loop back.
                continue

            # Nothing in flight. A still-stuck child here can't be waited on (its cursor exhausted
            # without ever delivering ``turn_end``) — give it up so the parent flows. Otherwise
            # every stream has quiesced/terminated with nothing stranded: the merge is done.
            if stuck_children:
                for workflow_id in stuck_children:
                    await self._give_up(workflow_id)
                continue
            return

    def _ensure_pulls(self) -> None:
        """Start a pull for every cursor that has no head, no in-flight pull, and isn't done."""
        for c in self._cursors.values():
            if c.head is None and not c.exhausted and c.pull_task is None:
                c.pull_task = asyncio.create_task(c.pull())

    async def _collect_completed_pulls(self) -> bool:
        """Reap finished pulls; stamp arrival order on new heads. Returns True iff the ROOT died.

        A pull that raised an unexpected error (e.g. ``CancelledError`` escaping teardown) still
        propagates. A CHILD whose pull marked it unreadable (``cursor.error`` — the concurrent-update
        cap, a completed child, etc.) OR ended cleanly (``exhausted``, its workflow completed) is
        handled GRACEFULLY via :meth:`_give_up`: drop it, release its close gates, and surface a
        marker if it abandoned a turn — so one over-subscribed/terminated subagent never crashes or
        wedges the merged stream. A failed ROOT is reported so the caller ends the stream cleanly."""
        departed_children: list[str] = []
        root_died = False
        for c in self._cursors.values():
            task = c.pull_task
            if task is None or not task.done():
                continue
            c.pull_task = None
            exc = task.exception()
            if exc is not None:
                # Not an expected read error (pull() captures those on cursor.error); let it out.
                raise exc
            if c.error is not None:
                # pull() captured a read error and marked the cursor exhausted.
                if c.is_child:
                    departed_children.append(c.workflow_id)
                else:
                    root_died = True
                continue
            if c.exhausted and c.head is None:
                # Clean end-of-stream (the workflow completed). A CHILD that ends — whether it
                # delivered everything or stopped mid-turn — gives up so any close gate still
                # waiting on it releases. A cleanly-ended ROOT is just the stream finishing; the
                # blocked-branch handles that (no parent event is stranded behind it).
                if c.is_child:
                    departed_children.append(c.workflow_id)
                continue
            if c.head is not None:
                c.head_seq = self._arrival_seq
                self._arrival_seq += 1
                # This stream just made progress, so clear any stall deadline armed against it:
                # a close-gate-blocking child is only given up when ITS OWN stream is silent for the
                # whole grace window. Re-armed fresh by ``_sync_stall_deadlines`` if it's still
                # blocking next iteration — so a child that keeps delivering is never given up,
                # while a sibling's deliveries never touch a different child's deadline.
                self._stall_deadlines.pop(c.workflow_id, None)
        for workflow_id in departed_children:
            await self._give_up(workflow_id)
        return root_died

    @staticmethod
    def _now() -> float:
        """Current event-loop time — the clock for the liveness stall deadlines ONLY.

        Never an ordering input (ordering is gate/offset-driven and clock-free); it only governs
        how long a close-gate-blocking child may stall before being given up."""
        return asyncio.get_running_loop().time()

    def _sync_stall_deadlines(self) -> set[str]:
        """Reconcile per-child stall deadlines with who is close-gate-blocking RIGHT NOW.

        Arms a fresh deadline (``now + stall_grace``) for each child that has just started blocking
        a buffered parent ``subagent_reply_received``, leaves an existing deadline UNCHANGED (so it
        counts down from when that child was last seen delivering — ``_collect_completed_pulls``
        clears the deadline on the child's own delivery, and it re-arms here — making the window
        per-child and immune to sibling chatter), and drops the deadline for any child that has
        stopped blocking (e.g. its ``turn_end`` finally arrived). Returns the set of
        currently-blocking child workflow_ids."""
        stuck = set(self._stuck_close_gate_children())
        for workflow_id in stuck:
            self._stall_deadlines.setdefault(workflow_id, self._now() + self._stall_grace)
        for workflow_id in list(self._stall_deadlines):
            if workflow_id not in stuck:
                del self._stall_deadlines[workflow_id]
        return stuck

    def _stuck_close_gate_children(self) -> list[str]:
        """Child workflow_ids that a buffered parent ``subagent_reply_received`` is close-gated on.

        Pure (no side effects). A non-empty result means the parent stream is held at a
        ``reply_received`` whose child ``turn_end`` hasn't been emitted — the one place a stalled or
        dead child can block the parent. The engine uses it both to decide whether to BOUND the wait
        and (after the wait stalls / nothing's in flight) to pick which children to give up on."""
        blocked: list[str] = []
        for c in self._cursors.values():
            head = c.head
            if (
                head is not None
                and isinstance(head.event, SubagentReplyReceived)
                and not self._gates.ready(
                    is_child=c.is_child, source_workflow_id=c.workflow_id, ev=head
                )
            ):
                blocked.append(head.event.workflow_id)
        return blocked

    async def _give_up(self, workflow_id: str) -> None:
        """Give up on a child stream: release its close gates, drop its cursor, and (if it abandoned
        an in-flight turn) queue a ``subagent_stream_unavailable`` marker. Idempotent.

        This is the single place the merge decides "we will not get this subagent's events." It is
        deliberately retry-free: recovery is a fresh attach (a page refresh), per design. The marker
        is emitted only when a turn was actually abandoned (its ``subagent_message_sent`` was emitted
        but its ``turn_end`` never was), so a child that simply finished and ended produces none."""
        self._stall_deadlines.pop(workflow_id, None)  # no longer waiting on it
        if workflow_id in self._gates.gone:
            # Already given up on (e.g. both a completed pull and a stuck gate referenced it).
            await self._unmount(workflow_id)
            return
        if self._abandoned_a_turn(workflow_id):
            self._pending_markers.append(self._unavailable_event(workflow_id))
        self._gates.mark_gone(workflow_id)
        await self._unmount(workflow_id)

    def _abandoned_a_turn(self, workflow_id: str) -> bool:
        """Whether we're dropping ``workflow_id`` with a turn whose detail we never delivered — its
        ``subagent_message_sent`` was emitted (turn ``opened``) but its child ``turn_end`` was not.
        That's exactly "we lost this subagent's turn detail," which is what the marker announces."""
        started = {t for (wf, t) in self._gates.opened if wf == workflow_id}
        ended = {t for (wf, t) in self._gates.child_turn_ended if wf == workflow_id}
        return bool(started - ended)

    def _unavailable_event(self, workflow_id: str) -> AgentEvent:
        """Synthesize the ``subagent_stream_unavailable`` marker for a given-up child.

        Stamped with ``agent_id == subagent_id`` so a UI that groups by ``agent_id`` routes it to
        the affected subagent's view, and a root-only consumer filters it out. ``turn_id`` is empty
        and ``timestamp`` 0.0 — it is a synthetic, non-turn signal (the merge is clock-free)."""
        subagent_id = self._subagent_ids.get(workflow_id, workflow_id)
        return AgentEvent(
            agent_id=subagent_id,
            turn_id="",
            turn_number=0,
            timestamp=0.0,
            event=SubagentStreamUnavailable(
                subagent_id=subagent_id,
                workflow_id=workflow_id,
                reason="subagent stream unavailable (its workflow has completed and is not yet "
                "replayable, or it stalled without delivering its turn) — refresh to retry",
            ),
        )

    async def _teardown(self) -> None:
        """Cancel in-flight pulls and close every subscription when the merge ends."""
        for c in self._cursors.values():
            await c.aclose()


async def merge_stream(
    *,
    client: Client,
    root_workflow_id: str,
    root_from_offset: int,
    skip_until_turn_id: str | None,
    select: SelectPolicy,
    should_stop: ShouldStop,
    stall_grace_seconds: float = DEFAULT_STALL_GRACE_SECONDS,
) -> AsyncIterator[MergedItem]:
    """Drive one gated k-way merge, yielding ``(event, resume_offset)`` pairs (see :data:`MergedItem`).

    Mounts ``root_workflow_id`` at ``root_from_offset``, then interleaves the root with every subagent
    stream it mounts on a ``subagent_message_sent``, recursively. ``skip_until_turn_id`` skips the root
    to a SPECIFIC turn's ``turn_started`` (``send_message``, which must land on the submitted turn even
    if its acceptance offset is mid a prior turn); ``None`` does no skipping — used by BOTH ``attach``
    from 0 (replay everything) and ``attach`` resume from an arbitrary offset (start exactly there).
    Resuming mid-stream is safe without skipping: a subagent whose turn began before ``root_from_offset``
    is never mounted (we never emit its ``subagent_message_sent``), so its events are absent and its
    later ``subagent_reply_received`` is released by the unmounted-stuck give-up; subagents dispatched
    at/after the offset mount and bracket-merge normally. ``select`` decides the order of
    genuinely-concurrent events; ``should_stop`` ends the run after a chosen terminal event. Always
    honors per-stream offset order and both brackets.

    A subagent stream that can't be read (a completed/stopped subagent, an over-subscribed
    workflow) or stalls is given up on — its close gate released so the parent flows, and a
    synthetic ``subagent_stream_unavailable`` marker emitted; the merge never retries (a fresh attach
    recovers). ``stall_grace_seconds`` bounds how long a stalled child may block a buffered parent
    reply before that give-up (a liveness backstop, not an ordering input)."""
    engine = _Merge(
        client=client,
        select=select,
        should_stop=should_stop,
        stall_grace_seconds=stall_grace_seconds,
    )
    async for item in engine.run(
        root_workflow_id=root_workflow_id,
        root_from_offset=root_from_offset,
        skip_until_turn_id=skip_until_turn_id,
    ):
        yield item
