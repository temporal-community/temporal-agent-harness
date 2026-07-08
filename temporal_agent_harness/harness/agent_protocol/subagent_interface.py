# ABOUTME: The sandbox-safe contract for the subagent-turn activity — the activity NAME, its
# input/output models, and the recommended timeouts. Part of the ``agent_protocol`` package
# (stdlib + pydantic only) so it imports cleanly inside the Temporal workflow sandbox, where
# the parent agent's runner lives and calls ``workflow.execute_activity``. The activity
# IMPLEMENTATION (which needs a Temporal ``Client`` + stream client) lives in
# ``harness/subagent_activities.py`` and is NOT sandbox-safe — the same protocol-vs-client
# split as ``agent_interface`` here vs ``agent_client``.

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from temporal_agent_harness.harness.agent_protocol.agent_interface import AgentConfig
from temporal_agent_harness.harness.stream_context import TurnStreamContext

# The registered name of the subagent-turn activity. Used by the activity's ``@activity.defn``
# (in ``subagent_activities.py``) and by the in-workflow ``send_<function>`` subagent tool's
# ``execute_activity`` call (in ``agent_workflow.py``).
RUN_SUBAGENT_TURN_ACTIVITY = "run_subagent_turn"

# Steady heartbeat cadence for the activity. The activity heartbeats every interval; the
# wrapper sets ``heartbeat_timeout`` to twice it, leaving one full interval of grace before
# Temporal would consider a silent worker dead. The activity self-derives the interval as
# ``heartbeat_timeout / 2`` (so the two stay consistent however the caller configures it).
DEFAULT_SUBAGENT_HEARTBEAT_INTERVAL = timedelta(seconds=10)
DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT = DEFAULT_SUBAGENT_HEARTBEAT_INTERVAL * 2

# Generous upper bound on a single subagent turn. Temporal REQUIRES a start_to_close (or
# schedule_to_close) timeout — ``heartbeat_timeout`` alone is rejected — so this is the ceiling
# while ``heartbeat_timeout`` does the real liveness detection. It is intentionally large (a
# subagent turn can run long); a dev wiring the subagent toolset can override it.
DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT = timedelta(hours=1)


class RunSubagentTurnInput(BaseModel):
    """Arguments for one subagent turn — sent to the activity by the parent's in-workflow
    ``send_<function>`` tool. There is deliberately NO turn-timeout field: the turn runs as long
    as the activity is allowed to (bounded only by the caller's ``start_to_close_timeout`` /
    liveness via ``heartbeat_timeout``)."""

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
    from_offset: int = Field(
        default=0,
        description="Where to begin consuming the child's stream — the caller's last-known "
        "offset (the previous turn's returned consumed_offset). A PERFORMANCE HINT ONLY, never "
        "correctness-critical: the streamer filters to this turn's turn_id, so a stale (smaller) "
        "offset merely replays a few already-seen events, and it can never be too large (the next "
        "turn's events always follow the prior turn's turn_end, where the prior consumed_offset "
        "points). So the caller need not — and must not — fetch the live stream head.",
    )
    # The following identify the dispatch for the SubagentMessageSent event the activity
    # publishes onto the PARENT's stream when it actually sends the message (see the activity).
    handle: str = Field(
        description="The parent's short handle for this subagent — for the SubagentMessageSent "
        "event the activity publishes on send."
    )
    agent_key: str = Field(
        description="The wired agent key of this subagent — for the SubagentMessageSent event."
    )
    parent_stream_context: TurnStreamContext = Field(
        description="The PARENT turn (turn_id + turn_number) to publish the SubagentMessageSent "
        "event against. The activity targets its own parent workflow's stream via "
        "publisher_from_activity; this carries which turn the dispatch belongs to."
    )


class SubagentTurnResult(BaseModel):
    """The outcome of one subagent turn — returned by the activity to the parent's
    ``send_<function>`` tool."""

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
    (``harness/subagent_transport.py`` — a same-cluster child workflow + the
    ``run_subagent_turn`` activity, the harness's default) and ``NexusTransport``
    (``nexus/subagents`` — an externally-fronted agent driven purely over Nexus operations, no
    activity). ``AgentWorkflowRunner`` (``agent_workflow.py``) holds one per subagent instance
    and delegates to it; it never branches on transport kind itself.

    Every method here takes only primitives, :class:`AgentConfig`, :class:`SubagentTurnResult`,
    and the already-leaf-module :class:`TurnStreamContext` — NEVER ``AgentWorkflowRunner`` or
    its internal subagent bookkeeping. That's deliberate, not incidental: it's what lets this
    ABC live in ``agent_protocol`` (stdlib + pydantic only, sandbox-safe) with zero dependency
    on ``agent_workflow.py`` in either direction — mirroring exactly why
    :class:`TurnStreamContext` itself is a standalone leaf type (see ``stream_context.py``'s own
    docstring). The runner does the work of unpacking its own state into these primitives
    before calling in, and of turning ``on_sent``'s callback back into a real
    ``SubagentMessageSent`` publish — that orchestration belongs in the runner, not here.
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
    async def send_turn(
        self,
        *,
        session_id: str,
        handle: str,
        agent_key: str,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        last_consumed_offset: int,
        stream_context: TurnStreamContext | None,
        on_sent: Callable[[int], None],
    ) -> SubagentTurnResult:
        """Drive one turn to completion and return its result.

        The caller supplies BOTH ``stream_context`` and ``on_sent``; use whichever mechanism
        applies to this transport and ignore the other:

        * ``stream_context`` is for a transport that dispatches an ACTIVITY to do the actual
          send — the activity can't get a synchronous callback out to workflow code mid-flight,
          so it needs this threaded into its own input to publish its dispatch marker
          autonomously (heartbeat-deduped, surviving retries). See ``ChildWorkflowTransport``.
        * ``on_sent`` is for a transport that sends directly from workflow code and so CAN
          report back synchronously the instant the send lands, called with the turn number the
          remote/child actually accepted. See ``NexusTransport``.
        """
        ...

    @abstractmethod
    async def stop(self, *, session_id: str) -> None:
        """Tear the instance down (signal ``close``, a remote operator command, ...)."""
        ...
