# ABOUTME: One mounted stream's cursor for the merge — a thin peek-ahead wrapper over a single
# agent workflow's ``WorkflowStream`` subscription. Holds at most one peeked ``head`` event, runs
# at most one in-flight ``pull`` task, and applies the root-only "skip to my turn_started" preamble
# that establishes the quiescent start point. The engine (merge.py) owns scheduling across cursors;
# this type owns only one stream's read position.

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta

from temporalio.client import Client
from temporalio.contrib.workflow_streams import (
    WorkflowStreamClient,
    WorkflowStreamItem,
)

from temporal_agent_harness.harness.agent_protocol import (
    TURN_EVENTS_TOPIC,
    AgentEvent,
    AgentEventType,
)

# subscribe() poll cadence — matches the legacy single-stream client (agent_client.py); keeps the
# merged stream snappy without hammering the workflow when items arrive faster than a poll.
_POLL_COOLDOWN = timedelta(milliseconds=10)

_log = logging.getLogger(__name__)


class Cursor:
    """A peek-ahead read position over one agent workflow's event stream.

    The engine keeps one ``Cursor`` per mounted agent (the root, plus each subagent mounted when
    its ``subagent_message_sent`` is emitted). Invariant: a cursor has EITHER a buffered
    :attr:`head` (the next event, awaiting emission) OR an in-flight :attr:`pull_task` fetching one
    OR is :attr:`exhausted` — never a head and a pull at once.
    """

    def __init__(
        self,
        *,
        workflow_id: str,
        is_child: bool,
        mount_index: int,
        events: AsyncIterator[WorkflowStreamItem[AgentEvent]],
        skip_until_turn_id: str | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        # False only for the root cursor — the open gate applies to child cursors only.
        self.is_child = is_child
        # Order this stream was mounted in (root = 0). The replay select policy ranks by it for a
        # deterministic, clock-free interleaving.
        self.mount_index = mount_index
        self._events = events
        # Root-only preamble (send_message): discard events until a SPECIFIC turn's ``turn_started``
        # — the submitted turn — which becomes the first head. ``None`` ⇒ no skipping, used by BOTH
        # ``attach`` from 0 (replay everything) AND ``attach`` resume from an arbitrary offset (start
        # exactly there; a subagent whose turn began earlier is simply never mounted — see the merge's
        # unmounted-stuck give-up). Children never skip.
        self._skip_until_turn_id = skip_until_turn_id
        self._skipping = skip_until_turn_id is not None

        self.head: AgentEvent | None = None
        # Offset of the buffered head in THIS stream's own log (the WorkflowStreamItem.offset). The
        # engine reads it as it emits each ROOT event to advance the ROOT-stream resume cursor it
        # hands back to the consumer (subagent heads don't move that cursor). -1 until a head is
        # buffered.
        self.head_offset: int = -1
        # Arrival order among completed pulls — the live select policy's tiebreak (set by engine).
        self.head_seq: int = -1
        self.exhausted = False
        self.pull_task: asyncio.Task[None] | None = None
        # Set when a pull raised something other than normal end-of-stream (e.g. the per-workflow
        # concurrent-update cap, or a transient RPC error). It marks the cursor unreadable so the
        # engine can DEGRADE GRACEFULLY — drop a failed CHILD cursor and keep coalescing the rest,
        # rather than letting one unreadable subagent crash the whole merged stream.
        self.error: BaseException | None = None

    @classmethod
    def mount(
        cls,
        client: Client,
        *,
        workflow_id: str,
        is_child: bool,
        mount_index: int,
        from_offset: int,
        skip_until_turn_id: str | None = None,
    ) -> "Cursor":
        """Open a subscription to ``workflow_id`` from ``from_offset`` and wrap it as a cursor."""
        events = WorkflowStreamClient.create(client, workflow_id).subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=from_offset,
            result_type=AgentEvent,
            poll_cooldown=_POLL_COOLDOWN,
        )
        return cls(
            workflow_id=workflow_id,
            is_child=is_child,
            mount_index=mount_index,
            events=events,
            skip_until_turn_id=skip_until_turn_id,
        )

    async def pull(self) -> None:
        """Advance the stream until :attr:`head` holds the next emittable event (or exhaust it).

        Honors the skip preamble: while skipping, events are discarded WITHOUT being emitted or
        recorded (so any prior-turn subagent brackets in that tail never enter the merge) until
        this turn's ``turn_started`` — which IS kept as the first head. ``subscribe()`` live-tails,
        so on an idle agent this simply awaits the next event; it raises ``StopAsyncIteration``
        only when the workflow has terminated, which sets :attr:`exhausted`.

        Any OTHER error (notably the per-workflow concurrent-in-flight-update cap, raised as an
        ``RPCError`` when too many cursors poll one workflow at once, or a poll against an
        already-completed child) is captured on :attr:`error` and marks the cursor exhausted rather
        than propagating — so the engine can drop this cursor and keep coalescing the rest. A
        ``CancelledError`` (teardown) is re-raised untouched.
        """
        try:
            while True:
                item = await anext(self._events)
                ev = item.data  # subscribe yields WorkflowStreamItem; .data is the AgentEvent
                if self._skipping:
                    if (
                        ev.event.type == AgentEventType.TURN_STARTED
                        and ev.turn_id == self._skip_until_turn_id
                    ):
                        self._skipping = False  # keep this turn_started as the first head
                    else:
                        continue  # discard the prior turn's tail
                self.head = ev
                self.head_offset = item.offset
                return
        except StopAsyncIteration:
            self.exhausted = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — see docstring: record, don't crash the merge
            self.error = exc
            self.exhausted = True
            _log.warning(
                "stream_merge: dropping cursor for workflow %s after a read error: %r",
                self.workflow_id,
                exc,
            )

    async def aclose(self) -> None:
        """Cancel any in-flight pull and close the underlying subscription (idempotent)."""
        if self.pull_task is not None and not self.pull_task.done():
            self.pull_task.cancel()
            try:
                await self.pull_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — teardown best-effort; never mask the real exit
                pass
        aclose = getattr(self._events, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 — best-effort
                pass
