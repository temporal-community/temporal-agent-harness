# ABOUTME: Stateless agent-turn client for Temporal workflow_streams workflows.
#
# Abstracts a single agent turn — sending a user message to a Temporal workflow,
# streaming intermediate tool events, and terminating when the turn ends (the
# workflow's turn_end event) — into a single async iterator. Designed to be
# resumable via a stream offset so that disconnects don't lose events.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any, TypeVar

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.contrib.workflow_streams import WorkflowStreamClient

from temporal_agent_harness.harness.agent_protocol import (
    AGENT_INTERFACE_QUERY,
    AGENT_STATUS_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    TOOL_APPROVAL_UPDATE,
    TURN_EVENTS_TOPIC,
    AcceptedFunction,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentStatus,
    PendingApproval,
    ToolApprovalDecision,
    ToolApprovalResult,
    AgentMessageReply,
)

# Client default: maximum seconds to wait for a turn to complete.
DEFAULT_TURN_TIMEOUT = 300.0

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AgentTurnTimeout(Exception):
    """Turn did not produce a terminal event before the timeout elapsed."""


class AgentTurnError(Exception):
    """Workflow published a terminal error event for the turn."""


class StaleTurnError(Exception):
    """Client's expected turn doesn't match the workflow's state.

    The client is behind — it should reattach to catch up before
    retrying.
    """


class AgentBusyError(Exception):
    """A turn is already in progress and message queuing is disabled."""


class ToolApprovalError(Exception):
    """A tool-approval decision was rejected by the workflow.

    Raised by :meth:`AgentClient.approve_tool` when the workflow's update validator
    rejects the decision — the ``tool_id`` is unknown, or the approval was already
    resolved (a double-submit). ``error_type`` carries the workflow-side type
    (``UnknownToolApproval`` / ``ToolApprovalAlreadyResolved``).
    """

    def __init__(self, message: str, *, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


# Must be defined after the exception classes since it references them at runtime.
AgentStreamOutput = AgentEvent | AgentTurnError | AgentTurnTimeout

# The callback signature: (item, offset) -> T
# offset is the stream offset of the event, so the caller can track position.
OnItemCallback = Callable[[AgentStreamOutput, int], T]


# ---------------------------------------------------------------------------
# AgentClient
# ---------------------------------------------------------------------------


class AgentClient:
    """Stateless client for interacting with an agent workflow.

    All durable state lives in the workflow (turn counter, liveness,
    stream log). This client is cheap to construct per-request.

    Args:
        temporal: Connected Temporal client.
        workflow_id: ID of the agent workflow to interact with.
    """

    def __init__(
        self,
        temporal: Client,
        workflow_id: str,
    ) -> None:
        self._temporal = temporal
        self._workflow_id = workflow_id

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    async def get_status(self) -> AgentStatus:
        """Query the workflow for its current status."""
        handle = self._temporal.get_workflow_handle(self._workflow_id)
        return await handle.query(AGENT_STATUS_QUERY, result_type=AgentStatus)

    async def get_pending_approvals(self) -> list[PendingApproval]:
        """The gated tool calls currently awaiting a human decision.

        A convenience over :meth:`get_status` — lets a client that attached after the
        ``tool_approval_requested`` event was published still discover and act on
        outstanding approvals (the event may have streamed by before it connected).
        """
        status = await self.get_status()
        return status.pending_approvals

    async def approve_tool(
        self,
        tool_id: str,
        *,
        approved: bool,
        reason: str | None = None,
        remember: bool = False,
    ) -> ToolApprovalResult:
        """Resolve a pending tool approval (see :class:`ToolApprovalRequested`).

        ``tool_id`` is the id carried by the ``tool_approval_requested`` event and listed
        under :attr:`AgentStatus.pending_approvals`. On approval the gated call dispatches
        immediately; on denial the model receives ``reason`` as the tool's error result.
        Resolving an unknown id, or one already resolved, raises :class:`ToolApprovalError`
        (the decision is idempotent — a double-submit fails rather than flipping it).

        ``remember=True`` (only meaningful with ``approved=True``) is "approve, and stop
        asking me about this tool": it adds the tool to the agent's live
        :class:`ToolApprovalPolicy` allow-list, so future calls of it skip the gate and any
        other call of that tool currently waiting auto-resolves. Read the updated policy
        back via :meth:`get_status` (``approval_policy``) to persist it for next session.
        """
        handle = self._temporal.get_workflow_handle(self._workflow_id)
        try:
            return await handle.execute_update(
                TOOL_APPROVAL_UPDATE,
                ToolApprovalDecision(
                    tool_id=tool_id, approved=approved, reason=reason, remember=remember
                ),
                result_type=ToolApprovalResult,
            )
        except WorkflowUpdateFailedError as e:
            cause = e.cause
            error_type = getattr(cause, "type", None) if cause else None
            if error_type in ("UnknownToolApproval", "ToolApprovalAlreadyResolved"):
                raise ToolApprovalError(str(cause), error_type=error_type) from e
            raise

    async def get_agent_interface(self) -> list[AcceptedFunction]:
        """Query the workflow for its callable surface — the ``@agent.accepts`` handlers.

        Returns one :class:`AcceptedFunction` per handler (``name`` / ``description`` /
        input ``parameters`` schema / ``output`` schema), Gemini-tool-shaped. Callers
        introspect this to construct a valid :class:`AgentMessage` for any handler (the
        ``name`` is the envelope ``type``), or — for a parent agent — to model each handler
        as a subagent tool, so the contract can evolve without client-side changes.
        """
        handle = self._temporal.get_workflow_handle(self._workflow_id)
        return await handle.query(
            AGENT_INTERFACE_QUERY, result_type=list[AcceptedFunction]
        )

    async def _submit_message(
        self,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
    ) -> AgentMessageReply:
        """Submit one message to the agent's front door, WITHOUT streaming the turn.

        Internal harness primitive — phase 1 of a turn. Public callers use :meth:`send_message`
        (the send-and-stream convenience); this and :meth:`_stream_turn` are the composable
        halves it is built from, reused in-package (e.g. by the subagent-turn activity, which
        sends then streams with its own dedup/heartbeat in between). Names the target
        ``@agent.accepts`` handler (``msg_type``), carries its input-model JSON (``payload``)
        and the ``expected_turn`` the caller believes this message is, builds the
        ``AgentMessage`` envelope internally, and forwards it as the ``send_agent_message``
        update. Returns the accepted :class:`AgentMessageReply` (``turn_id`` / ``turn_number`` /
        ``pending``).

        Raises:
            StaleTurnError: The client is behind the workflow.
            AgentBusyError: The agent is busy and does not support enqueuing.
        """
        handle = self._temporal.get_workflow_handle(self._workflow_id)
        try:
            return await handle.execute_update(
                SEND_AGENT_MESSAGE_UPDATE,
                AgentMessage(
                    type=msg_type, payload=payload, expected_turn=expected_turn
                ),
                result_type=AgentMessageReply,
            )
        except WorkflowUpdateFailedError as e:
            cause = e.cause
            error_type = getattr(cause, "type", None) if cause else None
            if error_type == "StaleTurn":
                raise StaleTurnError(str(cause)) from e
            if error_type == "AgentBusy":
                raise AgentBusyError(str(cause)) from e
            raise

    async def _stream_turn(
        self,
        *,
        turn_id: str,
        turn_number: int,
        on_item: OnItemCallback[T],
        from_offset: int = 0,
        timeout: float | None = DEFAULT_TURN_TIMEOUT,
    ) -> AsyncIterator[T]:
        """Stream a single (already-submitted) turn's events through to its completion.

        Internal harness primitive — phase 2 of a turn (pairs with :meth:`_submit_message`;
        public callers use :meth:`send_message`). Subscribes from ``from_offset`` and yields ALL
        events
        through to the completion of the turn identified by ``turn_id`` — including events
        from other turns the caller hasn't seen yet (e.g. interleaved turns from other
        callers). The stream terminates on ``turn_id``'s ``turn_end`` (the agent may emit
        events after its reply, so REPLY is NOT the end); that turn's error is surfaced as an
        :class:`AgentTurnError` item as it passes (streaming continues to the real ``turn_end``
        terminal). A caller can drop a prior stream and resume by passing the last offset it
        received as ``from_offset``.

        The ``on_item`` callback receives ``(item, offset)`` where ``offset`` is the next
        stream position; track it to resume later.

        ``timeout`` caps how long to wait for the turn to complete; ``None`` waits
        indefinitely (the turn runs as long as the caller allows). On a non-``None`` timeout
        an :class:`AgentTurnTimeout` item is yielded.
        """
        stream = WorkflowStreamClient.create(self._temporal, self._workflow_id)
        try:
            async with asyncio.timeout(timeout):
                async for item in stream.subscribe(
                    topics=[TURN_EVENTS_TOPIC],
                    from_offset=from_offset,
                    result_type=AgentEvent,
                    poll_cooldown=timedelta(milliseconds=10),
                ):
                    envelope: AgentEvent = item.data
                    payload = envelope.event
                    offset = item.offset + 1  # next offset for the caller
                    is_ours = envelope.turn_id == turn_id

                    # Surface OUR turn's error as the caller's failure signal
                    # (AgentTurnError) in place of the raw AgentError event, but
                    # keep streaming — turn_end (always emitted, even on error)
                    # is the real terminal.
                    if is_ours and payload.type == AgentEventType.ERROR:
                        yield on_item(
                            AgentTurnError(payload.message or "agent turn failed"),
                            offset,
                        )
                    else:
                        yield on_item(envelope, offset)

                    if is_ours and payload.type == AgentEventType.TURN_END:
                        return
        except TimeoutError:
            yield on_item(
                AgentTurnTimeout(
                    f"turn {turn_number} did not complete within {timeout}s"
                ),
                -1,
            )

    async def send_message(
        self,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        *,
        on_item: OnItemCallback[T],
        from_offset: int = 0,
        timeout: float | None = DEFAULT_TURN_TIMEOUT,
    ) -> AsyncIterator[T]:
        """Send a tool-call message and return a complete event stream.

        Composes the internal :meth:`_submit_message` (phase 1, eager — so ``StaleTurnError`` /
        ``AgentBusyError`` are raised *before* any streaming begins) with :meth:`_stream_turn`
        (phase 2, lazy — yields every event from ``from_offset`` through the submitted turn's
        ``turn_end``). This is the single public way to drive a turn; the two halves are private
        primitives the harness composes (and reuses in its own activities).

        Args:
            msg_type: Name of the target ``@agent.accepts`` handler.
            payload: JSON of that handler's input model.
            expected_turn: The turn number the client expects this message to be.
            on_item: Callback ``(AgentStreamOutput, offset) -> T`` applied to each output.
            from_offset: Pubsub offset to start streaming from (0 = everything; pass the last
                offset received from a prior stream to resume).
            timeout: Max seconds to wait for the turn to complete (``None`` = no limit).

        Returns:
            An async iterator of ``T``.

        Raises:
            StaleTurnError: The client is behind the workflow.
            AgentBusyError: The agent is busy and does not support enqueuing.
        """
        result = await self._submit_message(msg_type, payload, expected_turn)
        return self._stream_turn(
            turn_id=result.turn_id,
            turn_number=result.turn_number,
            on_item=on_item,
            from_offset=from_offset,
            timeout=timeout,
        )

    async def attach(
        self,
        *,
        on_item: OnItemCallback[T],
        from_offset: int = 0,
    ) -> AsyncIterator[T]:
        """Reattach to the event stream.

        The ``on_item`` callback receives ``(item, offset)`` where
        ``offset`` is the next stream position. The caller should track
        the latest offset so it can pass it as ``from_offset`` on the
        next call.

        When ``from_offset`` is 0, replays every past event (including
        ``TurnStarted`` with the original user message, so the UI can
        reconstruct the full conversation).

        When ``from_offset`` is non-zero, only events after that offset
        are yielded — ideal for periodic catch-up polling where the
        caller already has the earlier history.

        Returns immediately (yields nothing) if there are no new events
        and no in-flight work.

        Termination: ``turn_end`` is the sole end-of-turn signal (the
        workflow emits one per turn, and an agent may keep emitting events
        after its reply, so REPLY is NOT a terminal). On each ``turn_end``
        we re-query status and stop once ``turn_active`` is False and all
        turns through ``current_turn`` have ended.
        """
        stream = WorkflowStreamClient.create(self._temporal, self._workflow_id)

        status = await self.get_status()
        head = await stream.get_offset()

        # Nothing to stream — no history and no work, or already caught up.
        if head <= from_offset and not status.turn_active and not status.pending_turns:
            return
        if from_offset == 0 and status.current_turn == 0 and head == 0:
            return

        highest_completed_turn = 0

        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=from_offset,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            envelope: AgentEvent = item.data
            offset = item.offset + 1  # next offset for the caller

            yield on_item(envelope, offset)

            # turn_end is the ONLY terminal: the harness emits exactly one per turn
            # (success or error), and an agent may emit further events after its
            # reply — so we never stop at REPLY.
            if envelope.event.type == AgentEventType.TURN_END:
                highest_completed_turn = max(
                    highest_completed_turn, envelope.turn_number
                )
                try:
                    status = await self.get_status()
                except Exception:
                    return  # workflow is gone (e.g. it failed after an errored turn)
                if (
                    not status.turn_active
                    and highest_completed_turn >= status.current_turn
                ):
                    return
