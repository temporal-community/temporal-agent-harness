"""The swarm endpoints' reads: which swarm a session belongs to, and a swarm's whole topology.

A swarm can be hundreds of agents, so neither read touches an agent's workflow. Membership is
one ``describe``; the topology is ONE paged visibility query, ``SwarmId = '<id>'``, whose rows
already carry everything a member publishes about itself — its role, its parent (Temporal's own
``ParentWorkflowId``), its live phase and the peers it is waiting on (see
``harness/swarm_attributes.py``). The board is the one optional extra: a query per lane, for who
claimed what.

Visibility is eventually consistent, typically by about a second, so a topology is a recent
picture of the swarm rather than an exact one; ``as_of`` says when it was read.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from temporalio.client import Client, WorkflowExecution, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_protocol import (
    BOARD_SNAPSHOT_QUERY,
    BoardItemStatus,
    BoardSnapshot,
)
from temporal_agent_harness.harness.swarm_attributes import (
    AGENT_LAST_ACTIVE,
    AGENT_PEERS,
    AGENT_PHASE,
    AGENT_ROLE,
    BOARD_ROLE,
    SWARM_ID,
)
from temporal_agent_harness.web.session_manager import Session

# The most members one topology read returns. A swarm past it is reported as truncated rather
# than read without bound.
MAX_TOPOLOGY_NODES = 5000


class SwarmMembershipResponse(BaseModel):
    """The swarm a session's workflow belongs to, or ``swarm_id: null`` when it is in none."""

    workflow_id: str
    swarm_id: str | None
    role: str | None


class SwarmNode(BaseModel):
    """One member of a swarm, as its own search attributes describe it."""

    workflow_id: str
    workflow_type: str
    role: str | None
    parent_id: str | None
    phase: str | None = Field(
        description="The member's live AgentPhase; null for a member that publishes none (the "
        "board) or that closed before publishing one."
    )
    status: str = Field(description="The workflow's execution status, e.g. RUNNING, COMPLETED.")
    closed: bool
    start_time: float
    close_time: float | None
    last_active: float | None = Field(description="When the member's phase last changed.")


class SwarmEdge(BaseModel):
    """A relationship between two members.

    ``parent``: the target was started by the source, as its subagent. ``peer``: the source has
    a turn in flight to the target right now. ``board``: the source claimed items from the board
    lane that is the target; ``weight`` counts them."""

    source: str
    target: str
    kind: Literal["parent", "peer", "board"]
    weight: int = 1


class BoardLane(BaseModel):
    """One board lane's counts, when the topology was read with the board."""

    workflow_id: str
    lane: str
    counts: dict[BoardItemStatus, int]


class SwarmTopology(BaseModel):
    """A swarm's members and the relationships between them, read at ``as_of``."""

    swarm_id: str
    as_of: float
    truncated: bool = Field(description=f"Whether the swarm has more than {MAX_TOPOLOGY_NODES} members.")
    nodes: list[SwarmNode]
    edges: list[SwarmEdge]
    lanes: list[BoardLane] = Field(default_factory=list)
    phases: dict[str, int] = Field(
        description="Running members per phase ('none' for those without one)."
    )
    roles: dict[str, int] = Field(description="Members per role, running or not.")


def _timestamp(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _node(execution: WorkflowExecution) -> SwarmNode:
    attributes = execution.typed_search_attributes
    status = execution.status or WorkflowExecutionStatus.RUNNING
    return SwarmNode(
        workflow_id=execution.id,
        workflow_type=execution.workflow_type,
        role=attributes.get(AGENT_ROLE),
        parent_id=execution.parent_id,
        phase=attributes.get(AGENT_PHASE),
        status=status.name,
        closed=status != WorkflowExecutionStatus.RUNNING,
        start_time=execution.start_time.timestamp(),
        close_time=_timestamp(execution.close_time),
        last_active=_timestamp(attributes.get(AGENT_LAST_ACTIVE)),
    )


async def session_swarm(temporal: Client, workflow_id: str) -> SwarmMembershipResponse:
    """The swarm ``workflow_id`` belongs to, read from its own search attributes."""
    description = await temporal.get_workflow_handle(workflow_id).describe()
    attributes = description.typed_search_attributes
    return SwarmMembershipResponse(
        workflow_id=workflow_id,
        swarm_id=attributes.get(SWARM_ID),
        role=attributes.get(AGENT_ROLE),
    )


async def member_session(temporal: Client, workflow_id: str) -> Session:
    """Any agent workflow — a swarm member, or a subagent — described as a console session, so
    the console can open one that the session manager never listed. Labelled by its swarm role
    when it has one."""
    description = await temporal.get_workflow_handle(workflow_id).describe()
    role = description.typed_search_attributes.get(AGENT_ROLE)
    return Session(
        workflow_id=workflow_id,
        created_at=description.start_time.timestamp(),
        label=f"{role} · {description.workflow_type}" if role else description.workflow_type,
        agent_workflow_type=description.workflow_type,
        is_discovered=True,
    )


async def _lane(temporal: Client, workflow_id: str) -> BoardSnapshot | None:
    try:
        return await temporal.get_workflow_handle(workflow_id).query(
            BOARD_SNAPSHOT_QUERY, result_type=BoardSnapshot
        )
    except RPCError as e:
        # A lane that closed since visibility listed it has nothing more to say.
        if e.status in (RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION):
            return None
        raise


async def swarm_topology(
    temporal: Client, swarm_id: str, *, include_board: bool = False
) -> SwarmTopology:
    """Every member of ``swarm_id`` and the edges between them, from one visibility query.

    Members are deduplicated by workflow id, keeping the newest run: a board lane that has
    continued-as-new is listed once per run. ``include_board`` adds a snapshot query per running
    lane, for its counts and the members who claimed from it."""
    query = f"SwarmId = '{swarm_id.replace(chr(39), chr(39) * 2)}'"
    latest: dict[str, WorkflowExecution] = {}
    seen = 0
    truncated = False
    async for execution in temporal.list_workflows(query=query, limit=MAX_TOPOLOGY_NODES + 1):
        seen += 1
        if seen > MAX_TOPOLOGY_NODES:
            truncated = True
            break
        kept = latest.get(execution.id)
        if kept is None or execution.start_time > kept.start_time:
            latest[execution.id] = execution

    nodes = sorted((_node(e) for e in latest.values()), key=lambda n: n.start_time)
    members = {node.workflow_id for node in nodes}
    edges = [
        SwarmEdge(source=node.parent_id, target=node.workflow_id, kind="parent")
        for node in nodes
        if node.parent_id in members
    ]
    for execution in latest.values():
        if execution.status not in (None, WorkflowExecutionStatus.RUNNING):
            continue
        for peer in execution.typed_search_attributes.get(AGENT_PEERS) or []:
            if peer in members:
                edges.append(SwarmEdge(source=execution.id, target=peer, kind="peer"))

    lanes: list[BoardLane] = []
    if include_board:
        boards = [n.workflow_id for n in nodes if n.role == BOARD_ROLE and not n.closed]
        snapshots = await asyncio.gather(*(_lane(temporal, b) for b in boards))
        for workflow_id, snapshot in zip(boards, snapshots):
            if snapshot is None:
                continue
            lanes.append(
                BoardLane(workflow_id=workflow_id, lane=snapshot.lane, counts=snapshot.counts)
            )
            claims = Counter(i.claimed_by for i in snapshot.items if i.claimed_by in members)
            edges.extend(
                SwarmEdge(source=agent, target=workflow_id, kind="board", weight=count)
                for agent, count in claims.items()
            )

    return SwarmTopology(
        swarm_id=swarm_id,
        as_of=time.time(),
        truncated=truncated,
        nodes=nodes,
        edges=edges,
        lanes=lanes,
        phases=dict(Counter(n.phase or "none" for n in nodes if not n.closed)),
        roles=dict(Counter(n.role or "none" for n in nodes)),
    )
