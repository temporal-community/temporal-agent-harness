# ABOUTME: The harness subagent-turn activity — one activity that drives a single turn of a
# CHILD agent workflow: it sends the message to the child and streams the child's reply to
# completion, returning the reply payload. One activity call per subagent turn (cleaner than
# a send/consume split), with heartbeat state used as an "already sent?" memo so the common
# retry (a crash mid reply-stream) resumes *consuming* instead of re-sending.
#
# DESIGN — no stream-consume timeout + interval auto-heartbeat: we NEVER cap how long we wait
# for the subagent's terminal reply. A subagent may legitimately take arbitrarily long, and
# its stream events arrive at wildly varying cadences depending on the underlying agent. So
# instead of heartbeating off stream events (sparse, unpredictable), a background task
# heartbeats at a STEADY interval (derived as ``heartbeat_timeout / 2``, mirroring
# temporalio.contrib.openai_agents' auto-heartbeater) carrying the latest dedup memo. The
# activity's liveness is therefore the ``heartbeat_timeout`` (a short, predictable grace
# window — Temporal reaps a dead worker fast), NOT a guess at how long the turn "should" take.
# The activity runs until its ``start_to_close_timeout`` ceiling (set by the caller — Temporal
# requires one of the close timeouts; the toolset generator uses a generous default that devs
# can override).
#
# DESIGN — stream isolation: this activity reads the CHILD's stream ONLY to capture the reply
# and detect turn_end. It mirrors NONE of the child's content onto the parent agent's stream.
# (It does publish ONE marker of its own onto the PARENT's stream — the SubagentMessageSent
# dispatch event, when it actually sends the message — but that is the parent's own record, not
# any of the child's events; see _publish_dispatch.) A subagent's stream is never mirrored onto
# a parent's. Collecting multiple agents' streams for a UI is a future client concern.
#
# DESIGN — Temporal Client: the activity needs a ``Client`` to talk to
# the *child* (both the ``send_agent_message`` update and the stream subscribe). It can't use
# ``WorkflowStreamClient.from_within_activity()`` (that targets the activity's own parent).
# So this is a CLASS that closes over the worker's client; register the bound method as the
# activity (``activities=[SubagentActivities(client).run_subagent_turn]``). A future harness
# worker plugin will instantiate it from the worker's client automatically.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_client import (
    AgentBusyError,
    AgentClient,
    StaleTurnError,
)
from temporal_agent_harness.harness.agent_protocol import (
    DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
    RUN_SUBAGENT_TURN_ACTIVITY,
    TURN_EVENTS_TOPIC,
    AgentEvent,
    AgentEventType,
    RunSubagentTurnInput,
    SubagentMessageSent,
    SubagentTurnResult,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner


class _TurnProgress(BaseModel):
    """The activity's heartbeat memo — what a retry needs to resume without re-sending.

    Recorded once the message has been sent (``sent`` is always True when present), carrying
    the child's accepted ``turn_id`` / ``turn_number`` and the next stream ``consumed_offset``
    to resume from. Its presence in ``heartbeat_details`` is the "already sent?" signal; the
    background heartbeat task re-sends THIS object every interval, so the memo stays current as
    ``consumed_offset`` advances (and is never clobbered by an empty heartbeat).
    """

    sent: bool
    turn_id: str
    turn_number: int
    consumed_offset: int


class SubagentActivities:
    """Harness activities for driving subagents, bound to a Temporal :class:`Client`.

    Construct with the worker's client (closed over so the activity can talk to *child*
    workflows) and register the bound activity method on the worker::

        subagents = SubagentActivities(client)
        Worker(..., activities=[subagents.run_subagent_turn, ...])

    Kept a class (rather than a module-level client global) so the client is an explicit
    construction dependency; a future harness worker plugin instantiates this from the
    worker's client automatically.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn(name=RUN_SUBAGENT_TURN_ACTIVITY)
    async def run_subagent_turn(self, req: RunSubagentTurnInput) -> SubagentTurnResult:
        """Send one message to the child agent and stream its reply to completion.

        Sends the ``send_agent_message`` envelope to ``req.child_workflow_id`` (unless a
        heartbeat memo says a prior attempt already sent it), then subscribes to the child's
        stream — with NO timeout — captures the turn's :class:`AgentReply` output, and returns
        once that turn's ``turn_end`` arrives. A background task heartbeats the dedup memo at a
        steady interval throughout (see :meth:`_auto_heartbeat`). Mirrors none of the child's
        stream content onto the parent; the only thing published onto the parent's stream is the
        :class:`SubagentMessageSent` dispatch marker, on the fresh send (see :meth:`_publish_dispatch`).

        Failure modes surface as non-retryable :class:`ApplicationError` so the calling tool
        can render them as an ``is_error`` result to the parent model:

        * the child rejected the send (``StaleTurn`` / ``AgentBusy`` / ``UnknownFunction`` /
          ``MalformedMessage``) — the child's error ``type`` is preserved;
        * the turn ended in an error (``SubagentTurnError``);
        * the turn ended with no reply (``SubagentNoReply``).
        """
        client = AgentClient(self._client, req.child_workflow_id)

        # "Already sent?" memo: a retry that landed after the send resumes consuming from the
        # heartbeated offset instead of re-submitting the turn. (Best-effort, NOT fully
        # idempotent — a crash between the update returning and the first heartbeat being
        # durably recorded could still re-send; closing that residual window needs an
        # idempotent submit and is left as a future hardening pass.)
        progress = self._resume_progress()
        if progress is None:
            progress = await self._submit(client, req)
            # The send just happened — NOW publish the dispatch marker onto the PARENT's stream
            # (this is the accurate moment, vs. the parent's execute_activity dispatch time).
            # Only on the fresh-send branch: a heartbeat-resume retry skips both the re-send and
            # this publish, so the memo dedupes the event exactly as it dedupes the send.
            await self._publish_dispatch(req, progress)
        # Record/refresh the memo immediately (covers both the fresh-send and resume paths),
        # then let the background task keep it alive at a steady cadence.
        activity.heartbeat(progress)

        # Consume the CHILD's stream to capture this turn's reply. The auto-heartbeat keeps the
        # dedup memo alive at a steady cadence while we wait (no consume timeout — see header).
        async with self._auto_heartbeat(progress):
            output, got_reply = await self._consume_child_turn(req, progress)

        if not got_reply:
            # turn_end with no preceding reply — an error-only turn whose AgentError we
            # streamed past (or a turn that produced nothing). Surface as a tool error.
            # Carry the child's ACTUAL accepted turn number in the details: the parent closes
            # the [message_sent … reply_received] bracket on this exact turn number (the same
            # one the dispatch marker opened with), never a re-derived `expected` (see
            # AgentWorkflowRunner._accepted_turn_from_error).
            raise ApplicationError(
                f"subagent turn {progress.turn_number} ended without a reply",
                {"subagent_turn": progress.turn_number},
                type="SubagentNoReply",
                non_retryable=True,
            )
        return SubagentTurnResult(
            output=output,
            turn_id=progress.turn_id,
            turn_number=progress.turn_number,
            consumed_offset=progress.consumed_offset,
        )

    async def _consume_child_turn(
        self, req: RunSubagentTurnInput, progress: _TurnProgress
    ) -> tuple[dict[str, Any], bool]:
        """Stream ONE child turn's events to completion; return ``(reply_output, got_reply)``.

        The activity's minimal single-CHILD-stream reader — the replacement for the former
        ``AgentClient._stream_turn``. Subscribes the child's own stream from
        ``progress.consumed_offset`` (mutating it as events pass, so the auto-heartbeat memo stays
        current for a resume), filters to ``progress.turn_id``, captures the ``AgentReply`` output,
        and stops at that turn's ``turn_end``. The turn's terminal error is surfaced as a
        non-retryable ``SubagentTurnError``.

        Deliberately reads ONLY the child's stream — NO recursion into grandchildren and NO bracket
        gates. That is correct precisely because of stream isolation: coalescing the parent +
        subagent streams into one logical view is a separate CLIENT-side concern (``stream_merge``);
        an activity that gated on a grandchild's ``turn_end`` (a turn it never mounts) would wedge.
        """
        stream = WorkflowStreamClient.create(self._client, req.child_workflow_id)
        output: dict[str, Any] = {}
        got_reply = False
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=progress.consumed_offset,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            # Advance the resume offset for EVERY item seen (mutated in place so the background
            # heartbeat re-sends the latest), then act only on our turn's events.
            progress.consumed_offset = item.offset + 1
            envelope: AgentEvent = item.data
            if envelope.turn_id != progress.turn_id:
                continue
            payload = envelope.event
            if payload.type == AgentEventType.ERROR:
                # Carry the child's ACTUAL accepted turn number so the parent closes the bracket
                # on the same turn the dispatch marker opened (see _accepted_turn_from_error).
                raise ApplicationError(
                    payload.message or "subagent turn failed",
                    {"subagent_turn": progress.turn_number},
                    type="SubagentTurnError",
                    non_retryable=True,
                )
            if payload.type == AgentEventType.REPLY:
                output = payload.output
                got_reply = True
            if payload.type == AgentEventType.TURN_END:
                break
        return output, got_reply

    @asynccontextmanager
    async def _auto_heartbeat(self, progress: _TurnProgress) -> AsyncIterator[None]:
        """Heartbeat ``progress`` at a steady interval for the duration of the block.

        Mirrors ``temporalio.contrib.openai_agents``' auto-heartbeater: it heartbeats every
        ``heartbeat_timeout / 2`` so liveness is predictable even when the subagent's stream
        is silent for a long stretch (the activity is never mistaken for dead just because the
        turn is slow). ``progress`` is heartbeated by reference — mutating its
        ``consumed_offset`` in the consume loop keeps the dedup memo current — and it is never
        an empty heartbeat, so the "already sent?" memo is never clobbered. Falls back to a
        fixed interval if no ``heartbeat_timeout`` was configured (heartbeating is harmless).
        """
        # Heartbeat at half the configured timeout; if the caller configured none, fall back
        # to the harness default (the wrapper always sets one, so this is just insurance).
        heartbeat_timeout = (
            activity.info().heartbeat_timeout or DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT
        )
        interval = heartbeat_timeout.total_seconds() / 2

        async def beat() -> None:
            while True:
                await asyncio.sleep(interval)
                activity.heartbeat(progress)

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _submit(
        self, client: AgentClient, req: RunSubagentTurnInput
    ) -> _TurnProgress:
        """Submit the message to the child via the shared front door, and build the resume memo.

        Delegates the envelope build + update to :meth:`AgentClient._submit_message`, then
        translates a rejection into a non-retryable :class:`ApplicationError` that preserves
        the child's error ``type`` (``StaleTurn`` / ``AgentBusy`` / ``UnknownFunction`` /
        ``MalformedMessage``), so the calling tool can surface it verbatim. The memo seeds its
        ``consumed_offset`` from the caller-supplied ``req.from_offset`` (the perf hint — see
        :class:`RunSubagentTurnInput`); the stream then advances it from there.
        """
        try:
            result = await client._submit_message(
                req.type, req.payload, req.expected_turn
            )
        except StaleTurnError as e:
            raise ApplicationError(str(e), type="StaleTurn", non_retryable=True) from e
        except AgentBusyError as e:
            raise ApplicationError(str(e), type="AgentBusy", non_retryable=True) from e
        except WorkflowUpdateFailedError as e:
            cause = e.cause
            raise ApplicationError(
                str(cause) if cause else "subagent rejected the message",
                type=getattr(cause, "type", None) or "SubagentSendRejected",
                non_retryable=True,
            ) from e
        return _TurnProgress(
            sent=True,
            turn_id=result.turn_id,
            turn_number=result.turn_number,
            consumed_offset=req.from_offset,
        )

    @staticmethod
    async def _publish_dispatch(
        req: RunSubagentTurnInput, progress: _TurnProgress
    ) -> None:
        """Publish the :class:`SubagentMessageSent` marker onto the PARENT's stream.

        Called once per fresh send (see the caller's dedup note). Uses
        ``AgentWorkflowRunner.publisher_from_activity``, which targets THIS activity's own parent
        workflow (the agent that dispatched the activity) — so the event lands on the parent's
        stream, never the child's (stream isolation). ``subagent_turn`` is the child's actual
        accepted turn number from the send (``progress.turn_number``)."""
        async with AgentWorkflowRunner.publisher_from_activity(
            req.parent_stream_context
        ) as publisher:
            publisher.publish(
                SubagentMessageSent(
                    subagent_id=req.handle,
                    agent_key=req.agent_key,
                    workflow_id=req.child_workflow_id,
                    function=req.type,
                    subagent_turn=progress.turn_number,
                    # The child stream offset this turn's events begin at (the perf-hint offset
                    # the parent resumed from) — lets a client merging the parent + child streams
                    # mount the child cursor at the right spot when resuming mid-session.
                    from_offset=req.from_offset,
                )
            )

    @staticmethod
    def _resume_progress() -> _TurnProgress | None:
        """The most recent heartbeat memo for this activity attempt, or ``None`` if the
        message has not been sent yet. The pydantic converter decodes ``heartbeat_details``
        to plain values, so we re-validate the latest into :class:`_TurnProgress`."""
        details = activity.info().heartbeat_details
        if not details:
            return None
        return _TurnProgress.model_validate(details[-1])
