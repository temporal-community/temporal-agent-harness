# ABOUTME: The sandbox-safe contract for SWARMS — many agents cooperating as peers rather than
# one parent driving a tree of subagents. Three pieces, each the agent-side half of something a
# harness activity or a client implements:
#
#   * membership — ``SwarmMembership`` (carried on ``AgentConfig.swarm``) and ``AgentPhase``,
#     what a swarm agent advertises about itself through search attributes, so ONE visibility
#     query can list a whole swarm with its live state;
#   * peer turns — the ``run_peer_turn`` activity contract, the peer-to-peer analogue of
#     ``run_subagent_turn`` (see ``subagent_interface``) for addressing an agent you did not
#     start;
#   * the board — the shared work list a swarm coordinates through, and the one activity that
#     reaches it.
#
# Like the rest of ``agent_protocol`` this imports only stdlib + pydantic (plus the harness's
# own stream context), so the workflow-side runner can import it inside the sandbox. The search
# attribute KEYS live in ``harness/swarm.py``: they are Temporal SDK objects, not protocol.

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from temporal_agent_harness.harness.stream_context import TurnStreamContext

# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


class SwarmMembership(BaseModel):
    """Which swarm an agent belongs to, and the part it plays there.

    Set on ``AgentConfig.swarm`` to make an agent a swarm member. A member publishes its
    ``swarm_id``, ``role`` and live :class:`AgentPhase` as search attributes, and every
    subagent it starts joins the same swarm (with the subagent's key as its role unless the
    parent says otherwise). The namespace must have the swarm search attributes registered —
    see ``harness/swarm.py`` — or the member's first workflow task fails.
    """

    swarm_id: str = Field(
        min_length=1,
        description="The swarm this agent belongs to. Every member of one swarm shares it; it "
        "is what a client queries to list the swarm, and what names the swarm's board.",
    )
    role: str | None = Field(
        default=None,
        description="This agent's part in the swarm (e.g. 'lead', 'worker', 'reviewer'). Free "
        "text the swarm's author chooses; a console groups and colors agents by it. None for "
        "an agent with no particular role.",
    )


class AgentPhase(StrEnum):
    """What a swarm agent is doing right now, coarse enough to publish on every change.

    Derived by the runner from its own state, never set by the agent author, and published as
    the ``AgentPhase`` search attribute when it changes. When several apply, the one listed
    FIRST wins, because it is the one that explains why the agent is not making progress: an
    agent with a tool call waiting on a human is ``awaiting_approval`` even though that call is
    also a tool in flight.
    """

    AWAITING_APPROVAL = "awaiting_approval"
    """A tool call is gated on a human (or auto mode) decision."""

    AWAITING_CALLBACK = "awaiting_callback"
    """A callback tool call is waiting for the attached client to supply its result."""

    AWAITING_AGENT = "awaiting_agent"
    """A turn sent to another agent — a subagent or a peer — is in flight."""

    TOOL = "tool"
    """A tool call is running."""

    WORKING = "working"
    """A turn is open and none of the above applies: typically the model is generating."""

    IDLE = "idle"
    """No turn is open; the agent is waiting for its next message."""


def derive_agent_phase(
    *,
    turn_active: bool,
    pending_approvals: int,
    pending_callbacks: int,
    agent_turns_in_flight: int,
    tools_in_flight: int,
) -> AgentPhase:
    """The :class:`AgentPhase` for a runner in this state (first matching phase wins)."""
    if pending_approvals:
        return AgentPhase.AWAITING_APPROVAL
    if pending_callbacks:
        return AgentPhase.AWAITING_CALLBACK
    if agent_turns_in_flight:
        return AgentPhase.AWAITING_AGENT
    if tools_in_flight:
        return AgentPhase.TOOL
    if turn_active:
        return AgentPhase.WORKING
    return AgentPhase.IDLE


# ---------------------------------------------------------------------------
# Peer turns
# ---------------------------------------------------------------------------

# The registered name of the peer-turn activity (``SubagentActivities.run_peer_turn``).
RUN_PEER_TURN_ACTIVITY = "run_peer_turn"


class RunPeerTurnInput(BaseModel):
    """Arguments for one turn sent to a PEER — an agent the sender did not start.

    The peer counterpart of ``RunSubagentTurnInput``. The activity drives the peer through the
    same front door (``send_agent_message`` + its stream) and returns the same
    ``SubagentTurnResult``; what differs is the marker it publishes on the sender's stream
    (``peer_message_sent``, which a stream merge never mounts) and that the peer is addressed
    by its workflow id rather than a parent-owned handle.
    """

    peer_workflow_id: str = Field(description="The workflow id of the peer agent to message.")
    type: str = Field(
        description="The send_agent_message envelope 'type': the peer handler to route to."
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="The send_agent_message envelope 'payload': the handler's input JSON.",
    )
    from_offset: int = Field(
        default=0,
        description="Where to begin consuming the peer's stream — a performance hint only, "
        "exactly as RunSubagentTurnInput.from_offset.",
    )
    sender_stream_context: TurnStreamContext = Field(
        description="The SENDER's turn to publish the peer_message_sent marker against."
    )


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

# The registered name of the activity every board operation goes through
# (``SwarmActivities.swarm_board_update``).
SWARM_BOARD_UPDATE_ACTIVITY = "swarm_board_update"

# The board workflow's registered type and its update / query names.
SWARM_BOARD_WORKFLOW = "SwarmBoard"
BOARD_POST_UPDATE = "post"
BOARD_POST_MANY_UPDATE = "post_many"
BOARD_CLAIM_UPDATE = "claim"
BOARD_COMPLETE_UPDATE = "complete"
BOARD_RELEASE_UPDATE = "release"
BOARD_SNAPSHOT_QUERY = "snapshot"

BoardOperation = Literal["post", "post_many", "claim", "complete", "release"]

DEFAULT_BOARD_LANE = "main"
"""The lane a board call uses unless it names another."""


def swarm_board_workflow_id(swarm_id: str, lane: str = DEFAULT_BOARD_LANE) -> str:
    """The workflow id of one lane of ``swarm_id``'s board. Derived, so every member finds it.

    A swarm's board is split into LANES, each its own workflow, because one workflow takes a
    bounded rate of updates (and only ten in flight at once, by default): a swarm of hundreds
    claiming from a single workflow would queue on it. Give each group that works from its own
    pool of items — a team, a skill — its own lane, and their calls never contend."""
    return f"swarm-board-{swarm_id}-{lane}"


class BoardItemStatus(StrEnum):
    """Where one board item is in its life."""

    OPEN = "open"
    """Posted and waiting for an agent to claim it."""

    CLAIMED = "claimed"
    """An agent claimed it and has not yet completed or released it."""

    DONE = "done"
    """Completed; ``result`` holds what the completing agent reported."""


class BoardItem(BaseModel):
    """One unit of work on a swarm's board."""

    id: str = Field(description="The board-assigned id ('item-<n>'), unique within the board.")
    title: str = Field(description="A short human-readable summary of the work.")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Whatever the poster wants the claimer to have."
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Labels a claim can filter on (e.g. the team or skill the work needs).",
    )
    status: BoardItemStatus = BoardItemStatus.OPEN
    posted_by: str = Field(description="Workflow id of the agent that posted the item.")
    claimed_by: str | None = Field(
        default=None,
        description="Workflow id of the agent holding the item, while CLAIMED; and of the "
        "agent that completed it, once DONE.",
    )
    result: dict[str, Any] | None = Field(
        default=None, description="What the completing agent reported, once DONE."
    )
    attempts: int = Field(
        default=0, description="How many times the item has been claimed, including now."
    )
    last_release_reason: str | None = Field(
        default=None, description="Why the item was last released back to OPEN, if it was."
    )
    created_at: float = Field(description="When the item was posted (epoch seconds).")
    updated_at: float = Field(description="When the item last changed status (epoch seconds).")


class BoardPost(BaseModel):
    """Post one new item to the board."""

    title: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    posted_by: str


class BoardPostMany(BaseModel):
    """Post several items in one call (one update), in order."""

    posts: list[BoardPost] = Field(min_length=1)


class BoardPostManyResult(BaseModel):
    """The items a ``post_many`` created, in the order they were given."""

    items: list[BoardItem]


class BoardClaim(BaseModel):
    """Claim the oldest OPEN item, optionally only one carrying any of ``tags``."""

    agent_workflow_id: str
    tags: list[str] = Field(
        default_factory=list,
        description="Claim only an item carrying at least one of these. Empty claims any item.",
    )


class BoardClaimResult(BaseModel):
    """The outcome of a claim: the claimed item, or ``None`` when nothing matching was open."""

    item: BoardItem | None = None


class BoardComplete(BaseModel):
    """Complete an item the caller holds."""

    item_id: str
    agent_workflow_id: str
    result: dict[str, Any] = Field(default_factory=dict)


class BoardRelease(BaseModel):
    """Hand an item the caller holds back to OPEN, for another agent (or a later try)."""

    item_id: str
    agent_workflow_id: str
    reason: str | None = None


class BoardSnapshot(BaseModel):
    """One lane of the board as of one query."""

    swarm_id: str
    lane: str = DEFAULT_BOARD_LANE
    items: list[BoardItem] = Field(default_factory=list)
    counts: dict[BoardItemStatus, int] = Field(default_factory=dict)


class BoardState(BaseModel):
    """The board workflow's input and its carried state across continue-as-new."""

    swarm_id: str
    lane: str = DEFAULT_BOARD_LANE
    items: list[BoardItem] = Field(default_factory=list)
    next_number: int = 1


class SwarmBoardUpdateInput(BaseModel):
    """Arguments for the board activity: one operation against one lane of ``swarm_id``'s board.

    The activity creates the board on first use (update-with-start), on the task queue of the
    worker running the activity — so every worker that may run it must also host the board
    workflow, which ``AgentHarnessPlugin`` arranges.
    """

    swarm_id: str
    lane: str = DEFAULT_BOARD_LANE
    operation: BoardOperation
    argument: dict[str, Any] = Field(
        description="The operation's request (BoardPost / BoardPostMany / BoardClaim / "
        "BoardComplete / BoardRelease) as JSON."
    )
