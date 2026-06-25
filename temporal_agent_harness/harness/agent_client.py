# ABOUTME: Stateless agent-turn client for Temporal workflow_streams workflows.
#
# Abstracts a single agent turn — sending a user message to a Temporal workflow,
# streaming intermediate tool events, and terminating when the turn ends (the
# workflow's turn_end event) — into a single async iterator. Designed to be
# resumable via a stream offset so that disconnects don't lose events.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.contrib.workflow_streams import WorkflowStreamClient

from temporal_agent_harness.harness.agent_protocol import (
    AGENT_INTERFACE_QUERY,
    AGENT_STATUS_QUERY,
    EXECUTE_OPERATOR_COMMAND_UPDATE,
    OPERATOR_INTERFACE_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    TOOL_APPROVAL_UPDATE,
    AcceptedFunction,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentStatus,
    OperatorCommand,
    OperatorCommandRequest,
    OperatorCommandResult,
    PendingApproval,
    ToolApprovalDecision,
    ToolApprovalResult,
    AgentMessageReply,
)
from temporal_agent_harness.harness.stream_merge import (
    DEFAULT_STALL_GRACE_SECONDS,
    merge_stream,
    select_live,
    select_replay,
)
from temporal_agent_harness.harness.stream_merge.cursor import Cursor

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

# The callback signature: (item, resume_offset) -> T
# ``resume_offset`` is the merge's ROOT-stream resume CURSOR as of this item — a value the consumer
# records and hands back as ``attach(from_offset=...)`` to resume. It is NOT the event's own
# per-stream offset and NOT a merged display ordinal: it advances only on ROOT events, so every event
# within one subagent turn carries the same value (the cursor as of that turn's dispatch). See
# ``stream_merge.merge.MergedItem``.
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

    async def get_operator_interface(self) -> list[OperatorCommand]:
        """Query the workflow for operator-only slash command metadata.

        This is intentionally separate from :meth:`get_agent_interface`: operator commands
        are for UI/control-plane clients and must not become parent-agent tool surfaces.
        """
        handle = self._temporal.get_workflow_handle(self._workflow_id)
        return await handle.query(
            OPERATOR_INTERFACE_QUERY, result_type=list[OperatorCommand]
        )

    async def execute_operator_command(
        self, name: str, *, arg: str | None = None
    ) -> OperatorCommandResult:
        """Execute an operator-only command without creating an agent turn.

        This is the execution counterpart to :meth:`get_operator_interface`. It routes to
        the workflow's first-class operator update rather than ``send_agent_message``, so
        it can change runtime controls even while a model turn is busy.
        """
        handle = self._temporal.get_workflow_handle(self._workflow_id)
        return await handle.execute_update(
            EXECUTE_OPERATOR_COMMAND_UPDATE,
            OperatorCommandRequest(name=name, arg=arg),
            result_type=OperatorCommandResult,
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

    async def submit_message(
        self,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
    ) -> AgentMessageReply:
        """Submit one message to the agent without streaming the accepted turn.

        UI clients that maintain a separate ``attach`` stream should use this to avoid
        opening one long-lived stream per queued message. The returned
        :class:`AgentMessageReply` confirms the workflow accepted or queued the turn.
        """
        return await self._submit_message(msg_type, payload, expected_turn)

    async def send_message(
        self,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        *,
        on_item: OnItemCallback[T],
        timeout: float | None = DEFAULT_TURN_TIMEOUT,
        subagent_stall_grace_seconds: float = DEFAULT_STALL_GRACE_SECONDS,
    ) -> AsyncIterator[T]:
        """Send a message and stream the resulting turn — including any subagents — as ONE stream.

        Phase 1, :meth:`_submit_message`, runs eagerly here so ``StaleTurnError`` /
        ``AgentBusyError`` are raised *before* any streaming begins (and before the merge is even
        constructed — there is no failure path after the agent has accepted). The update returns an
        ``accepted_offset``; phase 2 then drives the client-side stream-merge from there: it skips
        to this turn's ``turn_started`` (a quiescent start) and yields every event of the turn,
        coalescing the agent's own stream with each subagent stream it drives (recursively), in a
        semantically-valid order — through to this turn's ``turn_end``. The caller never tracks or
        passes an offset.

        Args:
            msg_type: Name of the target ``@agent.accepts`` handler.
            payload: JSON of that handler's input model.
            expected_turn: The turn number the client expects this message to be.
            on_item: Callback ``(AgentStreamOutput, resume_offset) -> T`` applied to each output.
                ``resume_offset`` is the merge's ROOT-stream resume cursor as of this item (see
                :meth:`attach`); the per-turn path doesn't resume on it (the chat path reattaches via
                :meth:`attach`), but it is passed through uniformly so a caller's bookkeeping is
                consistent across both entry points.
            timeout: Max seconds to wait for the turn to complete (``None`` = no limit).
            subagent_stall_grace_seconds: Liveness backstop (seconds) for the stream merge — how
                long a subagent whose reply the parent is already waiting on may stay silent on its
                OWN stream before the merge presumes it unreachable, gives up on its detail, and
                lets the parent's reply flow. NOT an ordering input (ordering is clock-free); it
                only bounds the wait on a close-gate-blocking subagent. Raise it on slow/overloaded
                workers (a healthy-but-laggy subagent is given up only if it exceeds this); lower it
                for snappier degradation. A genuinely idle/slow subagent that ISN'T blocking the
                parent's reply is never affected.

        Returns:
            An async iterator of ``T``.

        Raises:
            StaleTurnError: The client is behind the workflow.
            AgentBusyError: The agent is busy and does not support enqueuing.
        """
        reply = await self._submit_message(msg_type, payload, expected_turn)
        return self._merged_turn(
            reply,
            on_item=on_item,
            timeout=timeout,
            stall_grace_seconds=subagent_stall_grace_seconds,
        )

    async def _merged_turn(
        self,
        reply: AgentMessageReply,
        *,
        on_item: OnItemCallback[T],
        timeout: float | None,
        stall_grace_seconds: float,
    ) -> AsyncIterator[T]:
        """Phase 2 of :meth:`send_message`: drive the merge for one submitted turn.

        Reads from ``reply.accepted_offset`` and skips to ``reply.turn_id``'s ``turn_started``,
        then merges live (arrival-order interleaving) until that turn's ``turn_end`` on the ROOT
        agent — by which point, via the close gate, every subagent turn it triggered has already
        been emitted. The turn's own terminal error is surfaced as an :class:`AgentTurnError`
        (matching the legacy behavior); on timeout an :class:`AgentTurnTimeout` is yielded last.
        """
        target_turn_id = reply.turn_id

        async def should_stop(cursor: Cursor, ev: AgentEvent) -> bool:
            return (
                not cursor.is_child
                and ev.turn_id == target_turn_id
                and ev.event.type == AgentEventType.TURN_END
            )

        merged = merge_stream(
            client=self._temporal,
            root_workflow_id=self._workflow_id,
            root_from_offset=reply.accepted_offset,
            skip_until_turn_id=target_turn_id,
            select=select_live,
            should_stop=should_stop,
            stall_grace_seconds=stall_grace_seconds,
        )
        try:
            async with asyncio.timeout(timeout):
                # ``resume_offset`` is the merge's ROOT-stream resume cursor; for a single
                # send_message turn it isn't used to resume (the chat path reattaches via ``attach``),
                # but we pass it through uniformly so the consumer's bookkeeping stays consistent.
                async for ev, resume_offset in merged:
                    # ``turn_id`` is a globally-unique uuid, so it alone identifies OUR turn's
                    # terminal error (a subagent's error carries a different turn_id) — no need to
                    # also match on agent_id.
                    if (
                        ev.turn_id == target_turn_id
                        and ev.event.type == AgentEventType.ERROR
                    ):
                        # Surface OUR turn's terminal error as the caller's failure signal in
                        # place of the raw AgentError event (the merge already streamed the
                        # subtree; turn_end still follows as the real terminal).
                        yield on_item(
                            AgentTurnError(ev.event.message or "agent turn failed"),
                            resume_offset,
                        )
                    else:
                        yield on_item(ev, resume_offset)
        except TimeoutError:
            yield on_item(
                AgentTurnTimeout(
                    f"turn {reply.turn_number} did not complete within {timeout}s"
                ),
                -1,
            )

    async def attach(
        self,
        *,
        on_item: OnItemCallback[T],
        from_offset: int = 0,
        subagent_stall_grace_seconds: float = DEFAULT_STALL_GRACE_SECONDS,
    ) -> AsyncIterator[T]:
        """Reattach to a session and stream it as ONE merged logical stream, then tail live.

        ``from_offset`` controls where the merge starts (and ANY offset is valid — see below):

        * **0 (default)** — full replay from the beginning, for a blank-slate consumer (a freshly
          loaded tab) that has no prior state. Replays past events deterministically (mount-order
          interleaving), then follows live until the agent is idle.
        * **any prior resume offset** — resume: stream from exactly that ROOT offset onward, so
          already-seen events are not re-sent. The one consequence of resuming *inside* a subagent's
          turn (an offset after that subagent's ``subagent_message_sent`` but before its
          ``subagent_reply_received``): that subagent's OWN events are absent from the resumed stream
          (the merge never saw its turn start, so it can't mount its stream), though the parent's
          ``subagent_reply_received`` and everything after it still flow. Subagents dispatched at or
          after ``from_offset`` merge normally.

        ``on_item`` receives ``(item, resume_offset)``. **``resume_offset`` is a ROOT-stream offset
        and advances ONLY on root events** — record the latest and pass it back as ``from_offset`` to
        resume. Two consequences a caller must not miss:

        * It is the root-stream position, NOT the merge's display ordinal — the cross-stream
          interleaving itself is not a resumable position.
        * Resume granularity is therefore **per-root-event, not per-subagent-event**. Every subagent
          event between two root events carries the SAME ``resume_offset`` (the position just past
          the preceding root event). So a consumer that disconnects *mid a subagent's turn* and
          resumes will NOT re-receive the rest of that subagent's turn detail — on resume the merge
          starts past that subagent's ``subagent_message_sent`` and so never re-mounts it (and emits
          NO ``subagent_stream_unavailable`` marker — it treats the detail as already delivered). The
          parent's reply and everything after it still flow. This is intended: a scalar root offset
          is a *valid* resume point (never a broken ordering, never a wedge), but it is lossless only
          at root-event granularity. A consumer that needs every subagent event across a mid-turn
          reconnect should re-attach from 0.

        Returns immediately (yields nothing) if there's nothing new to stream.

        ``subagent_stall_grace_seconds`` — see :meth:`send_message`; same liveness backstop, applied
        to the merge that backs this attach.

        Termination mirrors the per-turn close: ``turn_end`` is the sole end-of-turn signal, so on
        each ROOT ``turn_end`` we re-query status and stop once ``turn_active`` is False and all
        turns through ``current_turn`` have ended (a quiescent point with no open subagent brackets).
        """
        stream = WorkflowStreamClient.create(self._temporal, self._workflow_id)
        status = await self.get_status()
        head = await stream.get_offset()
        # Already caught up (no events past from_offset) and the agent is idle — nothing to stream.
        if head <= from_offset and not status.turn_active and not status.pending_turns:
            return self._empty()
        return self._merged_attach(
            on_item=on_item,
            from_offset=from_offset,
            stall_grace_seconds=subagent_stall_grace_seconds,
        )

    async def _empty(self) -> AsyncIterator[T]:
        """An async iterator that yields nothing (the no-history, idle ``attach`` case)."""
        return
        yield  # pragma: no cover — unreachable; makes this an async generator

    async def _merged_attach(
        self, *, on_item: OnItemCallback[T], from_offset: int, stall_grace_seconds: float
    ) -> AsyncIterator[T]:
        """Phase 2 of :meth:`attach`: drive the merge from ``from_offset`` to the next idle point.

        No skip at any offset: the merge starts the root exactly at ``from_offset`` (0 = replay
        everything). Resuming mid-stream is safe — a subagent whose turn began before ``from_offset``
        is never mounted (its detail is absent, its ``reply_received`` released by the merge's
        unmounted-stuck give-up), while subagents dispatched at/after it merge normally."""
        root_id = self._workflow_id
        highest_completed_turn = 0

        async def should_stop(cursor: Cursor, ev: AgentEvent) -> bool:
            nonlocal highest_completed_turn
            if cursor.is_child or ev.event.type != AgentEventType.TURN_END:
                return False
            highest_completed_turn = max(highest_completed_turn, ev.turn_number)
            try:
                status = await self.get_status()
            except Exception:  # noqa: BLE001 — workflow gone (e.g. failed after an errored turn)
                return True
            return (
                not status.turn_active
                and highest_completed_turn >= status.current_turn
            )

        merged = merge_stream(
            client=self._temporal,
            root_workflow_id=root_id,
            root_from_offset=from_offset,
            skip_until_turn_id=None,
            select=select_replay,
            should_stop=should_stop,
            stall_grace_seconds=stall_grace_seconds,
        )
        async for ev, resume_offset in merged:
            yield on_item(ev, resume_offset)
