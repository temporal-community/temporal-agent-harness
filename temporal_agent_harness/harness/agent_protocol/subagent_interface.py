# ABOUTME: The sandbox-safe contract for the subagent-turn activity — the activity NAME, its
# input/output models, and the recommended timeouts. Part of the ``agent_protocol`` package
# (stdlib + pydantic only) so it imports cleanly inside the Temporal workflow sandbox, where
# the parent agent's runner lives and calls ``workflow.execute_activity``. The activity
# IMPLEMENTATION (which needs a Temporal ``Client`` + stream client) lives in
# ``harness/subagent_activities.py`` and is NOT sandbox-safe — the same protocol-vs-client
# split as ``agent_interface`` here vs ``agent_client``.

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

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
