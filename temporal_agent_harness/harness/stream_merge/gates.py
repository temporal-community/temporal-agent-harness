# ABOUTME: The pure, deterministic heart of the stream-merge — the two happens-before
# "brackets" that make a merged multi-agent stream a semantically-possible ordering. No I/O, no
# asyncio, no Temporal: just two sets and the ``ready`` / ``on_emit`` functions over them, so the
# bracket invariants can be unit-tested against adversarial scripted interleavings in isolation.
#
# A subagent turn T on child C is nested inside a pair of markers on C's PARENT stream:
#   parent:  … subagent_message_sent(C, T) … subagent_reply_received(C, T) …
#   child:        ⌊ turn_started(T) … reply(T) … turn_end(T) ⌋   (between the two markers)
# Two causal edges make that nesting mandatory (not cosmetic):
#   * OPEN  — C cannot begin turn T before the message that triggered it was sent, so C's turn-T
#             events happen-after ``subagent_message_sent(C, T)``.
#   * CLOSE — the parent's run_subagent_turn blocks until it has consumed C through ``turn_end(T)``
#             before publishing ``subagent_reply_received(C, T)``, so everything the parent emits
#             at/after that marker happens-after C's whole turn T.
# Everything NOT related by a bracket may interleave freely; we deliberately keep no global order.

from __future__ import annotations

from dataclasses import dataclass, field

from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
    AgentEventType,
    SubagentMessageSent,
    SubagentReplyReceived,
    SubagentStopped,
)


@dataclass(frozen=True)
class MountChild:
    """A "mount this subagent stream" instruction returned by :meth:`Gates.on_emit`.

    ``on_emit`` stays pure — it never touches cursors or I/O — so instead of mounting a child
    itself it hands the engine the child ``workflow_id`` to mount and the ``from_offset`` (in the
    CHILD's own stream) to position the new cursor at, both lifted off the ``subagent_message_sent``
    that opened the bracket. The engine performs the actual (idempotent) mount.

    ``subagent_id`` (the child's short id, also off the ``subagent_message_sent``) lets the engine
    remember ``workflow_id → subagent_id``, so if it later has to give up on this child it can label
    the synthesized ``subagent_stream_unavailable`` marker with the id a UI knows the subagent by — even
    though the child delivered no events of its own to carry that id.
    """

    workflow_id: str
    from_offset: int
    subagent_id: str


@dataclass(frozen=True)
class UnmountChild:
    """An "unmount this subagent stream" instruction returned by :meth:`Gates.on_emit`.

    The symmetric counterpart to :class:`MountChild`: lifted off a ``subagent_stopped`` marker, it
    tells the engine to close that child's cursor and drop it from the active set — releasing the
    in-flight long-poll *update* the cursor holds against the (now stopped/completed) child
    workflow. This is what keeps in-flight poll-updates from piling up on a child across a long
    ``attach`` (or many overlapping ones) until Temporal's per-workflow concurrent-update cap (10)
    is hit. Safe per the design's ordering: ``subagent_stopped`` is emitted at a quiescent point —
    by then every one of that child's turns is already drained (its last ``subagent_reply_received``
    required the child's ``turn_end``), and a stopped subagent's id never reappears — so no gated
    event is ever stranded by the unmount.
    """

    workflow_id: str


@dataclass
class Gates:
    """The bracket state for one merge run: which child turns are open, which have ended.

    * ``opened`` — ``(child_workflow_id, subagent_turn)`` pairs whose ``subagent_message_sent`` has
      already been EMITTED on the merged stream (the open gate is satisfied).
    * ``child_turn_ended`` — ``(child_workflow_id, child_turn_number)`` pairs whose child
      ``turn_end`` has already been EMITTED (the close gate is satisfied).
    * ``gone`` — child ``workflow_id``s the engine has given up on (stream unreadable/stalled, or
      stopped). A gone child can no longer deliver a ``turn_end``, so its close gates must NOT
      strand the parent: a ``subagent_reply_received`` for a gone child is treated as satisfied.
      This is the load-bearing rule that keeps an unreachable subagent from wedging the parent —
      the parent's own stream already carries everything needed to render it; the child's turn
      events are supplementary detail we simply forgo.

    All three grow monotonically over a run (cheap ``(str, int)`` tuples / ``str``s). Membership —
    never a timestamp — is the sole ordering input besides each stream's own offset order.
    """

    opened: set[tuple[str, int]] = field(default_factory=set)
    child_turn_ended: set[tuple[str, int]] = field(default_factory=set)
    gone: set[str] = field(default_factory=set)

    def mark_gone(self, workflow_id: str) -> None:
        """Record that the engine has given up on child ``workflow_id`` — releasing its close gates.

        Called when a child cursor is dropped (unreadable, stalled, or stopped). Idempotent. After
        this, any buffered ``subagent_reply_received`` for that child becomes emittable so the
        parent stream — and everything sequenced after it — can flow."""
        self.gone.add(workflow_id)

    def ready(
        self, *, is_child: bool, source_workflow_id: str, ev: AgentEvent
    ) -> bool:
        """Whether ``ev`` (the head of a cursor) may be emitted now, per the brackets.

        ``is_child`` / ``source_workflow_id`` describe the cursor ``ev`` came from. An event is
        held (returns ``False``) when emitting it would violate a happens-before edge; the engine
        leaves it as the cursor's head and revisits it once another stream supplies the enabling
        event. Anything not gated here is free to emit in any cross-stream order.
        """
        # OPEN gate: nothing on a child stream may surface before the parent's message_sent for
        # THAT turn has been emitted (which records the turn in ``opened``).
        if is_child and (source_workflow_id, ev.turn_number) not in self.opened:
            return False
        # CLOSE gate: a parent's reply_received waits for the referenced child turn's turn_end to
        # have been emitted — so the subagent's whole turn precedes the parent observing its reply.
        # EXCEPTION: if the engine has marked that child ``gone`` (its stream is unreachable), the
        # turn_end will never come, so we release the gate — the parent's reply (and everything
        # after it) must not be stranded behind detail we can no longer fetch.
        if isinstance(ev.event, SubagentReplyReceived):
            key = (ev.event.workflow_id, ev.event.subagent_turn)
            if key not in self.child_turn_ended and ev.event.workflow_id not in self.gone:
                return False
        return True

    def on_emit(
        self, *, is_child: bool, source_workflow_id: str, ev: AgentEvent
    ) -> MountChild | UnmountChild | None:
        """Record the effect of having just EMITTED ``ev``; maybe ask the engine to (un)mount a child.

        Updates the gate sets so subsequently-held events become ready:
          * a ``subagent_message_sent`` opens its child turn's gate AND returns a
            :class:`MountChild` so the engine mounts (idempotently) the child's stream;
          * a child ``turn_end`` satisfies the close gate for the parent's matching reply_received;
          * a ``subagent_stopped`` returns an :class:`UnmountChild` so the engine closes that child's
            cursor and releases its in-flight poll update (the child is drained + idle by then).
        Returns ``None`` when no (un)mount is needed.
        """
        if isinstance(ev.event, SubagentMessageSent):
            self.opened.add((ev.event.workflow_id, ev.event.subagent_turn))
            return MountChild(
                workflow_id=ev.event.workflow_id,
                from_offset=ev.event.from_offset,
                subagent_id=ev.event.subagent_id,
            )
        if isinstance(ev.event, SubagentStopped):
            return UnmountChild(workflow_id=ev.event.workflow_id)
        if is_child and ev.event.type == AgentEventType.TURN_END:
            self.child_turn_ended.add((source_workflow_id, ev.turn_number))
        return None
