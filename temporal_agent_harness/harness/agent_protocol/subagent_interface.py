# ABOUTME: The sandbox-safe contract for the subagent-turn activities — activity NAMES,
# input/output models, and the recommended timeouts, for the harness's built-in
# ChildWorkflowTransport. Part of the ``agent_protocol`` package (stdlib + pydantic only) so it
# imports cleanly inside the Temporal workflow sandbox, where the parent agent's runner lives
# and calls ``workflow.execute_activity``. The activity IMPLEMENTATIONS (which need a Temporal
# ``Client`` + stream client) live in ``harness/subagent_activities.py`` and are NOT
# sandbox-safe — the same protocol-vs-client split as ``agent_interface`` here vs
# ``agent_client``.
#
# TWO activities, not one: submitting a turn (a quick update-send) and consuming it to
# completion (a potentially long-running stream read) are split so that a parent workflow can
# learn the accepted turn number as soon as the send is confirmed — via the submit activity's
# plain return value — without waiting for the reply. That's what lets
# ``SubagentTransport.dispatch``/``await_reply`` (below) stay this simple: no callback, no
# "which turn to publish against" context threaded into either activity's input — the WORKFLOW
# already knows the turn number the moment ``dispatch`` returns, and publishes
# ``SubagentMessageSent`` itself, in workflow code, exactly like a Nexus-brokered transport
# already could. (An earlier version of this used one combined activity plus a
# ``parent_stream_context`` field threaded into its input so the activity could self-publish —
# that's gone; there's no longer anything for an activity to self-report, since the submit
# activity is short enough that the workflow can just await it directly.)

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from temporal_agent_harness.harness.agent_protocol.agent_interface import AgentConfig

# The registered names of the two subagent-turn activities. Used by their ``@activity.defn``s
# (in ``subagent_activities.py``) and by ``ChildWorkflowTransport``'s ``execute_activity`` calls
# (in ``subagent_transport.py``).
SUBMIT_SUBAGENT_TURN_ACTIVITY = "submit_subagent_turn"
CONSUME_SUBAGENT_TURN_ACTIVITY = "consume_subagent_turn"

# Steady heartbeat cadence for the consume activity (the only one long-running enough to need
# one). It heartbeats every interval; the wrapper sets ``heartbeat_timeout`` to twice it,
# leaving one full interval of grace before Temporal would consider a silent worker dead.
DEFAULT_SUBAGENT_HEARTBEAT_INTERVAL = timedelta(seconds=10)
DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT = DEFAULT_SUBAGENT_HEARTBEAT_INTERVAL * 2

# Generous upper bound on consuming a single subagent turn. Temporal REQUIRES a
# start_to_close (or schedule_to_close) timeout — ``heartbeat_timeout`` alone is rejected — so
# this is the ceiling while ``heartbeat_timeout`` does the real liveness detection. It is
# intentionally large (a subagent turn can run long); a dev wiring the subagent toolset can
# override it.
DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT = timedelta(hours=1)

# The submit activity is just one update-send — no stream to wait on — so it gets its own
# short, fixed ceiling rather than the consume activity's generous one.
DEFAULT_SUBAGENT_SUBMIT_START_TO_CLOSE_TIMEOUT = timedelta(seconds=30)


class SubmitSubagentTurnInput(BaseModel):
    """Arguments for submitting one subagent turn — sent to the activity by
    ``ChildWorkflowTransport.dispatch``. Deliberately carries none of ``handle``/``agent_key``/
    any stream-publishing context: the workflow publishes ``SubagentMessageSent`` itself, from
    this activity's plain return value, so the activity has nothing to self-report."""

    child_workflow_id: str = Field(
        description="The child agent workflow this turn targets — the subagent the parent "
        "started via its start_<key> tool and is now addressing."
    )
    type: str = Field(
        description="The send_agent_message envelope 'type': the name of the target "
        "@agent.accepts handler on the child to route this turn to."
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="The send_agent_message envelope 'payload': the JSON of the target "
        "handler's input model.",
    )
    expected_turn: int = Field(
        description="The parent's locally tracked next turn number for this subagent; the "
        "child rejects the send as stale if it doesn't match its own next turn."
    )


class SubmitSubagentTurnResult(BaseModel):
    """The child's confirmation that it accepted the turn — returned to the parent workflow
    as soon as the send lands, well before the child necessarily has a reply ready."""

    turn_id: str = Field(description="The id of the turn the child accepted.")
    turn_number: int = Field(description="The number of the turn the child accepted.")


class ConsumeSubagentTurnInput(BaseModel):
    """Arguments for consuming an already-submitted subagent turn to completion — sent to the
    activity by ``ChildWorkflowTransport.await_reply``. ``turn_id``/``turn_number`` come from
    the matching ``SubmitSubagentTurnResult``; there is deliberately no turn-timeout field —
    the turn runs as long as the activity is allowed to (bounded only by the caller's
    ``start_to_close_timeout``/liveness via ``heartbeat_timeout``)."""

    child_workflow_id: str = Field(description="The child agent workflow this turn targets.")
    turn_id: str = Field(description="The id of the turn to consume, from SubmitSubagentTurnResult.")
    turn_number: int = Field(
        description="The number of the turn to consume, from SubmitSubagentTurnResult."
    )
    from_offset: int = Field(
        default=0,
        description="Where to begin consuming the child's stream — the caller's last-known "
        "offset (the previous turn's returned consumed_offset). A PERFORMANCE HINT ONLY, never "
        "correctness-critical: the streamer filters to this turn's turn_id, so a stale (smaller) "
        "offset merely replays a few already-seen events, and it can never be too large (the next "
        "turn's events always follow the prior turn's turn_end, where the prior consumed_offset "
        "points). So the caller need not — and must not — fetch the live stream head.",
    )


class SubagentTurnResult(BaseModel):
    """The outcome of one subagent turn — returned by the consume activity (or, for a
    Nexus-brokered transport, by ``await_reply_over_nexus``) to the parent's ``send_<function>``
    tool."""

    output: dict[str, Any] = Field(
        default_factory=dict,
        description="The child handler's reply, as the raw JSON dict carried on the child's "
        "AgentReply. The calling send_<function> tool re-validates it against the handler's "
        "statically known output model (boundary validation).",
    )
    turn_id: str = Field(description="The id of the turn the child actually ran.")
    turn_number: int = Field(
        description="The number of the turn the child actually ran."
    )
    consumed_offset: int = Field(
        description="The child stream position just past this turn's turn_end. The caller "
        "stores it and threads it back as the next turn's from_offset, so each turn streams "
        "from where the last one ended (cheap resume, no full-history replay)."
    )


class SubagentTransport(ABC):
    """How a parent starts, drives, and stops one subagent instance — the harness's pluggable
    transport seam. Two implementations exist: ``ChildWorkflowTransport``
    (``harness/subagent_transport.py`` — a same-cluster child workflow + the submit/consume
    activities above, the harness's default) and ``NexusTransport`` (``nexus/subagents`` — an
    externally-fronted agent driven purely over Nexus operations, no activity).
    ``AgentWorkflowRunner`` (``agent_workflow.py``) holds one per subagent instance and
    delegates to it; it never branches on transport kind itself.

    ``dispatch``/``await_reply`` split "send" from "wait for the reply" as two separately
    awaited calls, specifically so the CALLER always learns the accepted turn number as soon as
    it's confirmed — via ``dispatch``'s plain return value — without waiting for the reply, and
    can publish ``SubagentMessageSent`` itself right then, in workflow code. Neither method
    takes anything shaped like ``AgentWorkflowRunner`` or its internal subagent bookkeeping —
    only primitives, :class:`AgentConfig`, and :class:`SubagentTurnResult`. That's deliberate,
    not incidental: it's what lets this ABC live in ``agent_protocol`` (stdlib + pydantic only,
    sandbox-safe) with zero dependency on ``agent_workflow.py`` in either direction.
    """

    @abstractmethod
    async def start(
        self,
        *,
        handle: str,
        agent_key: str,
        session_id: str,
        config: AgentConfig | None,
    ) -> None:
        """Start (or, for a lazily-started remote agent, merely accept) the instance that will
        be addressed as ``session_id`` from here on — already minted by the caller, so every
        transport agrees on the same id whether it's a real child ``workflow_id`` or a remote
        session id."""
        ...

    @abstractmethod
    async def dispatch(
        self,
        *,
        session_id: str,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
    ) -> int:
        """Send the message; return the turn number the moment it's confirmed accepted.

        Raises if the child/remote agent rejects the send outright (stale turn, busy, unknown
        function, ...) — in that case nothing was accepted, so the caller publishes nothing and
        advances no bookkeeping. A successful return means ``await_reply`` can now be called
        for this same turn."""
        ...

    @abstractmethod
    async def await_reply(
        self,
        *,
        session_id: str,
        turn_number: int,
        last_consumed_offset: int,
    ) -> SubagentTurnResult:
        """Wait for the turn most recently confirmed by ``dispatch`` to complete, and return
        its result. ``turn_number`` is the value ``dispatch`` returned — implementations may
        assert it matches what they privately recorded, since a caller must always pair one
        ``await_reply`` with the ``dispatch`` that immediately preceded it (the runner's
        per-subagent FIFO gate guarantees the two are never interleaved with another turn)."""
        ...

    @abstractmethod
    async def stop(self, *, session_id: str) -> None:
        """Tear the instance down (signal ``close``, a remote operator command, ...)."""
        ...

    def nexus_endpoint(self) -> str | None:
        """Non-None if this instance's stream needs a Nexus poll loop instead of a same-cluster
        WorkflowStreamClient subscription (see harness/stream_merge). Default None; overridden
        by NexusTransport."""
        return None
