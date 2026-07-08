# ABOUTME: The harness's two subagent-turn activities — submit (a quick update-send) and
# consume (stream the child's reply to completion) — backing ChildWorkflowTransport. Split so
# the parent workflow learns the accepted turn number as soon as submit returns, without
# waiting for consume; see agent_protocol/subagent_interface.py's module docstring for why that
# matters (SubagentMessageSent gets published by the WORKFLOW, from submit's plain return
# value — neither activity self-publishes anything onto the parent's stream anymore).
#
# DESIGN — submit's own idempotency: a single activity.heartbeat() right after a successful
# send, checked at the top of the NEXT attempt. If the activity attempt crashes after the
# child accepted the send but before the result reaches the workflow, Temporal retries the
# SAME activity task, which sees its own prior heartbeat and returns the memoized result
# instead of re-sending (which the child would reject anyway, as a stale turn, since it already
# advanced past `expected_turn` — but better to detect and skip than to attempt and fail).
# Same residual gap the original combined activity always had: a crash between the update
# actually landing and the heartbeat call recording it is not covered — closing that needs an
# idempotent submit and is left as a future hardening pass, exactly as before.
#
# DESIGN — consume's heartbeat: unlike submit, there is no "already sent?" question here (there
# is no send) — the heartbeat exists purely for LIVENESS (Temporal reaps a dead worker fast)
# and, as a bonus, lets a retry resume from the last-seen offset instead of `from_offset` again
# (cheap either way — a stale offset just replays a few already-seen events — but resuming is
# less wasteful after a long-running turn).
#
# DESIGN — stream isolation: consume reads the CHILD's stream ONLY to capture the reply and
# detect turn_end. It mirrors NONE of the child's content onto the parent agent's stream. A
# subagent's stream is never mirrored onto a parent's — collecting multiple agents' streams for
# a UI is a future client concern.
#
# DESIGN — Temporal Client: both activities need a ``Client`` to talk to the *child* (update +
# stream subscribe). Neither can use ``WorkflowStreamClient.from_within_activity()`` (that
# targets the activity's own parent). So this is a CLASS that closes over the worker's client;
# register the bound methods as activities
# (``activities=[SubagentActivities(client).submit_subagent_turn, ...consume_subagent_turn]``).

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

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
    CONSUME_SUBAGENT_TURN_ACTIVITY,
    DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
    SUBMIT_SUBAGENT_TURN_ACTIVITY,
    TURN_EVENTS_TOPIC,
    AgentEvent,
    AgentEventType,
    ConsumeSubagentTurnInput,
    SubagentTurnResult,
    SubmitSubagentTurnInput,
    SubmitSubagentTurnResult,
)


class _ConsumeProgress(BaseModel):
    """Consume's heartbeat memo — just the resume offset, mutated in place as the stream
    advances so the background heartbeat always sends the latest."""

    consumed_offset: int


class SubagentActivities:
    """Harness activities for driving subagents, bound to a Temporal :class:`Client`.

    Construct with the worker's client (closed over so the activities can talk to *child*
    workflows) and register the bound activity methods on the worker::

        subagents = SubagentActivities(client)
        Worker(..., activities=[subagents.submit_subagent_turn, subagents.consume_subagent_turn, ...])
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn(name=SUBMIT_SUBAGENT_TURN_ACTIVITY)
    async def submit_subagent_turn(
        self, req: SubmitSubagentTurnInput
    ) -> SubmitSubagentTurnResult:
        """Send one message to the child agent and return its accepted turn id/number.

        Idempotent-ish across retries via a heartbeat memo (see module docstring) — a retry
        that lands after a successful send returns the memoized result instead of re-sending.

        Failure modes surface as non-retryable :class:`ApplicationError` so the calling tool
        can render them as an ``is_error`` result to the parent model: the child rejected the
        send (``StaleTurn`` / ``AgentBusy`` / ``UnknownFunction`` / ``MalformedMessage``) — the
        child's error ``type`` is preserved.
        """
        details = activity.info().heartbeat_details
        if details:
            memo = SubmitSubagentTurnResult.model_validate(details[-1])
            return memo

        client = AgentClient(self._client, req.child_workflow_id)
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
        memo = SubmitSubagentTurnResult(turn_id=result.turn_id, turn_number=result.turn_number)
        activity.heartbeat(memo)
        return memo

    @activity.defn(name=CONSUME_SUBAGENT_TURN_ACTIVITY)
    async def consume_subagent_turn(self, req: ConsumeSubagentTurnInput) -> SubagentTurnResult:
        """Stream the child's reply to completion for an already-submitted turn.

        Subscribes to the child's stream — with NO timeout — captures the turn's
        :class:`AgentReply` output, and returns once that turn's ``turn_end`` arrives. A
        background task heartbeats the resume offset at a steady interval throughout (see
        :meth:`_auto_heartbeat`).

        Failure modes surface as non-retryable :class:`ApplicationError`: the turn ended in an
        error (``SubagentTurnError``), or ended with no reply (``SubagentNoReply``).
        """
        details = activity.info().heartbeat_details
        resume_offset = (
            _ConsumeProgress.model_validate(details[-1]).consumed_offset
            if details
            else req.from_offset
        )
        progress = _ConsumeProgress(consumed_offset=resume_offset)

        async with self._auto_heartbeat(progress):
            output, got_reply = await self._consume_child_turn(req, progress)

        if not got_reply:
            raise ApplicationError(
                f"subagent turn {req.turn_number} ended without a reply",
                type="SubagentNoReply",
                non_retryable=True,
            )
        return SubagentTurnResult(
            output=output,
            turn_id=req.turn_id,
            turn_number=req.turn_number,
            consumed_offset=progress.consumed_offset,
        )

    async def _consume_child_turn(
        self, req: ConsumeSubagentTurnInput, progress: _ConsumeProgress
    ) -> tuple[dict, bool]:
        """Stream ONE child turn's events to completion; return ``(reply_output, got_reply)``.

        Deliberately reads ONLY the child's stream — NO recursion into grandchildren and NO
        bracket gates. That is correct precisely because of stream isolation: coalescing the
        parent + subagent streams into one logical view is a separate CLIENT-side concern
        (``stream_merge``); an activity that gated on a grandchild's ``turn_end`` (a turn it
        never mounts) would wedge.
        """
        stream = WorkflowStreamClient.create(self._client, req.child_workflow_id)
        output: dict = {}
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
            if envelope.turn_id != req.turn_id:
                continue
            payload = envelope.event
            if payload.type == AgentEventType.ERROR:
                raise ApplicationError(
                    payload.message or "subagent turn failed",
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
    async def _auto_heartbeat(self, progress: _ConsumeProgress) -> AsyncIterator[None]:
        """Heartbeat ``progress`` at a steady interval for the duration of the block.

        Mirrors ``temporalio.contrib.openai_agents``' auto-heartbeater: it heartbeats every
        ``heartbeat_timeout / 2`` so liveness is predictable even when the subagent's stream
        is silent for a long stretch. ``progress`` is heartbeated by reference — mutating its
        ``consumed_offset`` in the consume loop keeps the memo current. Falls back to a fixed
        interval if no ``heartbeat_timeout`` was configured (heartbeating is harmless).
        """
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
