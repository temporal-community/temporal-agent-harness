# ABOUTME: Tests for swarms — the search attributes a member publishes (its swarm, role and live
# phase, which a subagent inherits), peer turns between agents that did not start each other, and
# the swarm board. The rules (phase precedence, the board's state machine) are tested directly;
# the rest runs model-free agents against a local dev server with the swarm search attributes
# registered.
#
# Run with: uv run pytest tests/harness/test_swarm.py -v

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.common import TypedSearchAttributes
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    BOARD_SNAPSHOT_QUERY,
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AgentPhase,
    BoardClaim,
    BoardComplete,
    BoardItemStatus,
    BoardPost,
    BoardPostMany,
    BoardRelease,
    BoardSnapshot,
    BoardState,
    SwarmMembership,
    derive_agent_phase,
    swarm_board_workflow_id,
)
from temporal_agent_harness.harness.swarm import _Board
from temporal_agent_harness.harness.swarm_attributes import (
    AGENT_LAST_ACTIVE,
    AGENT_PEERS,
    AGENT_PHASE,
    AGENT_ROLE,
    SWARM_ID,
    SWARM_SEARCH_ATTRIBUTES,
    dev_server_flags,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin
from tests.harness._swarm_agents import SwarmFounderAgent, SwarmTestAgent

# ---------------------------------------------------------------- phase precedence


def _phase(**state: int | bool) -> AgentPhase:
    base: dict[str, int | bool] = {
        "turn_active": False,
        "pending_approvals": 0,
        "pending_callbacks": 0,
        "agent_turns_in_flight": 0,
        "tools_in_flight": 0,
    }
    return derive_agent_phase(**{**base, **state})  # type: ignore[arg-type]


def test_phase_is_idle_with_no_turn_and_working_with_one():
    assert _phase() is AgentPhase.IDLE
    assert _phase(turn_active=True) is AgentPhase.WORKING


def test_phase_names_what_blocks_progress_first():
    # A gated tool call is also a tool in flight; the approval is what explains the wait.
    assert (
        _phase(turn_active=True, tools_in_flight=1, pending_approvals=1)
        is AgentPhase.AWAITING_APPROVAL
    )
    assert (
        _phase(turn_active=True, tools_in_flight=1, pending_callbacks=1)
        is AgentPhase.AWAITING_CALLBACK
    )
    # A subagent or peer tool call is a tool too; the other agent is what it waits on.
    assert (
        _phase(turn_active=True, tools_in_flight=1, agent_turns_in_flight=1)
        is AgentPhase.AWAITING_AGENT
    )
    assert _phase(turn_active=True, tools_in_flight=2) is AgentPhase.TOOL


def test_dev_server_flags_register_every_swarm_attribute():
    flags = dev_server_flags()
    assert flags[::2] == ["--search-attribute"] * len(SWARM_SEARCH_ATTRIBUTES)
    assert flags[1::2] == [
        "SwarmId=Keyword",
        "AgentRole=Keyword",
        "AgentPhase=Keyword",
        "AgentLastActive=Datetime",
        "AgentPeers=KeywordList",
    ]


# ---------------------------------------------------------------- board rules


def _board() -> _Board:
    return _Board(BoardState(swarm_id="s"))


def test_claims_take_the_oldest_matching_open_item():
    board = _board()
    a = board.post(BoardPost(title="a", tags=["x"], posted_by="p"), now=1)
    b = board.post(BoardPost(title="b", tags=["y"], posted_by="p"), now=2)
    assert (a.id, b.id) == ("item-1", "item-2")

    claimed = board.claim(BoardClaim(agent_workflow_id="w1", tags=["y"]), now=3).item
    assert claimed is not None and claimed.id == b.id
    assert claimed.status is BoardItemStatus.CLAIMED and claimed.claimed_by == "w1"
    assert claimed.attempts == 1

    untagged = board.claim(BoardClaim(agent_workflow_id="w2"), now=4).item
    assert untagged is not None and untagged.id == a.id
    assert board.claim(BoardClaim(agent_workflow_id="w3"), now=5).item is None


def test_a_released_item_keeps_its_place_and_counts_attempts():
    board = _board()
    first = board.post(BoardPost(title="first", posted_by="p"), now=1)
    board.post(BoardPost(title="second", posted_by="p"), now=2)
    board.claim(BoardClaim(agent_workflow_id="w1"), now=3)
    released = board.release(
        BoardRelease(item_id=first.id, agent_workflow_id="w1", reason="busy"), now=4
    )
    assert released.status is BoardItemStatus.OPEN and released.claimed_by is None
    assert released.last_release_reason == "busy"

    again = board.claim(BoardClaim(agent_workflow_id="w2"), now=5).item
    assert again is not None and again.id == first.id and again.attempts == 2


def test_only_the_holder_completes_or_releases_an_item():
    board = _board()
    item = board.post(BoardPost(title="t", posted_by="p"), now=1)
    with pytest.raises(ApplicationError) as unclaimed:
        board.complete(BoardComplete(item_id=item.id, agent_workflow_id="w1"), now=2)
    assert unclaimed.value.type == "BoardItemNotHeld"

    board.claim(BoardClaim(agent_workflow_id="w1"), now=3)
    with pytest.raises(ApplicationError) as other:
        board.release(BoardRelease(item_id=item.id, agent_workflow_id="w2"), now=4)
    assert other.value.type == "BoardItemNotHeld"
    with pytest.raises(ApplicationError) as unknown:
        board.complete(BoardComplete(item_id="item-99", agent_workflow_id="w1"), now=5)
    assert unknown.value.type == "UnknownBoardItem"

    done = board.complete(
        BoardComplete(item_id=item.id, agent_workflow_id="w1", result={"ok": True}), now=6
    )
    assert done.status is BoardItemStatus.DONE and done.result == {"ok": True}
    assert done.claimed_by == "w1"


def test_post_many_posts_in_order_and_lanes_have_their_own_ids():
    board = _board()
    result = board.post_many(
        BoardPostMany(posts=[BoardPost(title=t, posted_by="p") for t in "abc"]), now=1
    )
    assert [(i.id, i.title) for i in result.items] == [
        ("item-1", "a"),
        ("item-2", "b"),
        ("item-3", "c"),
    ]
    assert swarm_board_workflow_id("s") == "swarm-board-s-main"
    assert swarm_board_workflow_id("s", "billing") == "swarm-board-s-billing"


def test_board_state_round_trips_through_continue_as_new():
    board = _board()
    board.post(BoardPost(title="a", posted_by="p"), now=1)
    board.claim(BoardClaim(agent_workflow_id="w"), now=2)
    resumed = _Board(board.state())
    assert resumed.snapshot() == board.snapshot()
    assert resumed.post(BoardPost(title="b", posted_by="p"), now=3).id == "item-2"
    assert resumed.snapshot().counts == {
        BoardItemStatus.OPEN: 1,
        BoardItemStatus.CLAIMED: 1,
        BoardItemStatus.DONE: 0,
    }


# ---------------------------------------------------------------- against a server


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(
        plugins=[AgentHarnessPlugin()], search_attributes=SWARM_SEARCH_ATTRIBUTES
    )
    yield env
    await env.shutdown()


@pytest_asyncio.fixture
async def task_queue(env: WorkflowEnvironment) -> AsyncIterator[str]:
    queue = f"swarm-{uuid.uuid4()}"
    async with Worker(
        env.client, task_queue=queue, workflows=[SwarmTestAgent, SwarmFounderAgent]
    ):
        yield queue


async def _start(
    client: Client, task_queue: str, *, swarm: SwarmMembership | None = None
) -> str:
    handle = await client.start_workflow(
        "SwarmTestAgent",
        AgentConfig(swarm=swarm),
        id=f"swarm-agent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    return handle.id


async def _turn(
    client: Client, workflow_id: str, handler: str, payload: dict
) -> list[AgentEvent]:
    """Send one message and return its turn's events, through ``turn_end``."""
    reply: AgentMessageReply = await client.get_workflow_handle(workflow_id).execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=handler, payload=payload),
        result_type=AgentMessageReply,
    )
    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, workflow_id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        if envelope.turn_id != reply.turn_id:
            continue
        events.append(envelope)
        if envelope.event.type == AgentEventType.TURN_END:
            return events
    raise AssertionError("stream ended before turn_end")


def _reply(events: list[AgentEvent]) -> dict:
    ends = [e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END]
    assert ends, f"no reply; got {[e.event.type for e in events]}"
    return ends[0].output


def _error(events: list[AgentEvent]) -> str:
    errors = [e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_ERROR]
    assert errors, f"no error; got {[e.event.type for e in events]}"
    return errors[0].message


async def _attributes(client: Client, workflow_id: str) -> TypedSearchAttributes:
    return (await client.get_workflow_handle(workflow_id).describe()).typed_search_attributes


async def _eventually(check: Callable[[], object], *, timeout: float = 10.0) -> None:
    """Retry the async ``check`` until it stops raising AssertionError."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            await check()  # type: ignore[misc]
            return
        except AssertionError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.2)


async def test_a_member_publishes_its_swarm_role_and_phase(env, task_queue):
    swarm = SwarmMembership(swarm_id=f"s-{uuid.uuid4().hex[:8]}", role="coordinator")
    agent_id = await _start(env.client, task_queue, swarm=swarm)
    assert _reply(await _turn(env.client, agent_id, "echo", {"text": "hi"})) == {
        "text": "echo: hi"
    }

    async def idle_member() -> None:
        attrs = await _attributes(env.client, agent_id)
        assert attrs.get(SWARM_ID) == swarm.swarm_id
        assert attrs.get(AGENT_ROLE) == "coordinator"
        assert attrs.get(AGENT_PHASE) == AgentPhase.IDLE.value
        assert attrs.get(AGENT_LAST_ACTIVE) is not None

    await _eventually(idle_member)


async def test_a_non_member_publishes_no_swarm_attributes(env, task_queue):
    agent_id = await _start(env.client, task_queue)
    await _turn(env.client, agent_id, "echo", {"text": "hi"})
    attrs = await _attributes(env.client, agent_id)
    assert attrs.get(SWARM_ID) is None and attrs.get(AGENT_PHASE) is None


async def test_phase_shows_a_member_waiting_on_an_approval(env, task_queue):
    swarm = SwarmMembership(swarm_id=f"s-{uuid.uuid4().hex[:8]}")
    agent_id = await _start(env.client, task_queue, swarm=swarm)
    turn = asyncio.create_task(_turn(env.client, agent_id, "gated", {"text": "go"}))

    async def awaiting() -> None:
        attrs = await _attributes(env.client, agent_id)
        assert attrs.get(AGENT_PHASE) == AgentPhase.AWAITING_APPROVAL.value

    await _eventually(awaiting)
    await AgentClient(env.client, agent_id).approve_tool("gated-1", approved=True)
    assert _reply(await turn) == {"text": "go"}

    async def idle() -> None:
        assert (await _attributes(env.client, agent_id)).get(AGENT_PHASE) == "idle"

    await _eventually(idle)


async def test_a_subagent_joins_its_parents_swarm(env, task_queue):
    swarm = SwarmMembership(swarm_id=f"s-{uuid.uuid4().hex[:8]}", role="lead")
    parent_id = await _start(env.client, task_queue, swarm=swarm)
    await _turn(env.client, parent_id, "spawn", {"task_queue": task_queue})
    status = await AgentClient(env.client, parent_id).get_status()
    (child,) = status.subagents

    child_attrs = await _attributes(env.client, child.workflow_id)
    assert child_attrs.get(SWARM_ID) == swarm.swarm_id
    assert child_attrs.get(AGENT_ROLE) == "worker"

    # One visibility query lists the swarm, parent links included.
    async def listed() -> None:
        members = {
            execution.id: execution.parent_id
            async for execution in env.client.list_workflows(
                f"SwarmId = '{swarm.swarm_id}'"
            )
        }
        assert members == {parent_id: None, child.workflow_id: parent_id}

    await _eventually(listed)


async def test_ask_peer_relays_the_reply_and_records_the_edge(env, task_queue):
    asker = await _start(env.client, task_queue)
    peer = await _start(env.client, task_queue)
    events = await _turn(
        env.client, asker, "ask", {"peer": peer, "handler": "echo", "text": "ping"}
    )
    assert _reply(events) == {"text": "echo: ping"}

    sent = [e.event for e in events if e.event.type == AgentEventType.PEER_MESSAGE_SENT]
    received = [e.event for e in events if e.event.type == AgentEventType.PEER_REPLY_RECEIVED]
    assert [(e.peer_workflow_id, e.handler, e.peer_turn) for e in sent] == [(peer, "echo", 1)]
    assert [(e.peer_workflow_id, e.peer_turn, e.outcome) for e in received] == [
        (peer, 1, "ok")
    ]
    # Never the subagent markers: a stream merge must not mount a peer.
    assert not any(e.event.type.startswith("subagent_") for e in events)


async def test_a_member_advertises_the_peers_it_is_waiting_on(env, task_queue):
    swarm = SwarmMembership(swarm_id=f"s-{uuid.uuid4().hex[:8]}")
    asker = await _start(env.client, task_queue, swarm=swarm)
    peer = await _start(env.client, task_queue)
    turn = asyncio.create_task(
        _turn(env.client, asker, "ask", {"peer": peer, "handler": "slow_echo", "text": "zz"})
    )

    async def edge() -> None:
        assert (await _attributes(env.client, asker)).get(AGENT_PEERS) == [peer]

    await _eventually(edge)
    assert _reply(await turn) == {"text": "echo: zz"}

    async def no_edge() -> None:
        attrs = await _attributes(env.client, asker)
        assert not attrs.get(AGENT_PEERS) and attrs.get(AGENT_PHASE) == "idle"

    await _eventually(no_edge)


async def test_a_peer_turn_that_fails_closes_its_edge_with_an_error(env, task_queue):
    asker = await _start(env.client, task_queue)
    peer = await _start(env.client, task_queue)
    events = await _turn(
        env.client, asker, "ask", {"peer": peer, "handler": "fail", "text": "boom"}
    )
    assert "failing on purpose: boom" in _error(events)
    received = [e.event for e in events if e.event.type == AgentEventType.PEER_REPLY_RECEIVED]
    assert [(e.peer_turn, e.outcome) for e in received] == [(1, "error")]


async def test_a_subagent_turn_that_fails_closes_its_bracket(env, task_queue):
    parent = await _start(env.client, task_queue)
    events = await _turn(env.client, parent, "spawn_and_fail", {"task_queue": task_queue})
    # The child's own failure, not the activity wrapper's "Activity task failed".
    assert "failing on purpose: child" in _error(events)
    received = [
        e.event for e in events if e.event.type == AgentEventType.SUBAGENT_REPLY_RECEIVED
    ]
    assert [(e.subagent_turn, e.outcome) for e in received] == [(1, "error")]


async def test_ask_peer_refuses_itself_and_a_peer_that_is_not_running(env, task_queue):
    asker = await _start(env.client, task_queue)
    itself = await _turn(
        env.client, asker, "ask", {"peer": asker, "handler": "echo", "text": "me"}
    )
    assert "cannot message itself" in _error(itself)

    missing = await asyncio.wait_for(
        _turn(env.client, asker, "ask", {"peer": "no-such-agent", "handler": "echo", "text": "?"}),
        timeout=30,
    )
    assert "is not running" in _error(missing)


async def test_a_member_works_the_swarm_board(env, task_queue):
    swarm = SwarmMembership(swarm_id=f"s-{uuid.uuid4().hex[:8]}")
    agent_id = await _start(env.client, task_queue, swarm=swarm)
    report = _reply(
        await _turn(env.client, agent_id, "work_board", {"titles": ["one", "two", "three"]})
    )
    assert report == {
        "posted": ["item-1", "item-2", "item-3"],
        "completed": "item-1",
        "released": "item-2",
        "empty_claim": True,
    }

    board_id = swarm_board_workflow_id(swarm.swarm_id)  # the default lane
    snapshot = await env.client.get_workflow_handle(board_id).query(
        BOARD_SNAPSHOT_QUERY, result_type=BoardSnapshot
    )
    assert [(i.id, i.status, i.posted_by) for i in snapshot.items] == [
        ("item-1", BoardItemStatus.DONE, agent_id),
        ("item-2", BoardItemStatus.OPEN, agent_id),
        ("item-3", BoardItemStatus.OPEN, agent_id),
    ]
    assert snapshot.items[0].claimed_by == agent_id
    board_attrs = await _attributes(env.client, board_id)
    assert board_attrs.get(SWARM_ID) == swarm.swarm_id
    assert board_attrs.get(AGENT_ROLE) == "board"


async def test_the_board_is_unreachable_outside_a_swarm(env, task_queue):
    agent_id = await _start(env.client, task_queue)
    events = await _turn(env.client, agent_id, "work_board", {"titles": ["x"]})
    assert "not a swarm member" in _error(events)


async def test_an_agent_founds_its_own_swarm_unless_its_caller_placed_it(env, task_queue):
    founder = (
        await env.client.start_workflow(
            "SwarmFounderAgent",
            AgentConfig(),
            id=f"founder-{uuid.uuid4()}",
            task_queue=task_queue,
        )
    ).id
    ids = _reply(await _turn(env.client, founder, "seed", {"lane": "a", "titles": ["x", "y"]}))
    assert ids == {"text": "item-1,item-2"}
    attrs = await _attributes(env.client, founder)
    assert (attrs.get(SWARM_ID), attrs.get(AGENT_ROLE)) == (founder, "founder")
    snapshot = await env.client.get_workflow_handle(
        swarm_board_workflow_id(founder, "a")
    ).query(BOARD_SNAPSHOT_QUERY, result_type=BoardSnapshot)
    assert (snapshot.lane, [i.title for i in snapshot.items]) == ("a", ["x", "y"])

    placed = SwarmMembership(swarm_id=f"s-{uuid.uuid4().hex[:8]}", role="guest")
    guest = (
        await env.client.start_workflow(
            "SwarmFounderAgent",
            AgentConfig(swarm=placed),
            id=f"founder-{uuid.uuid4()}",
            task_queue=task_queue,
        )
    ).id
    await _turn(env.client, guest, "seed", {"lane": "a", "titles": ["z"]})
    attrs = await _attributes(env.client, guest)
    assert (attrs.get(SWARM_ID), attrs.get(AGENT_ROLE)) == (placed.swarm_id, "guest")


async def test_stopping_a_subagent_that_already_closed_succeeds(env, task_queue):
    parent = await _start(env.client, task_queue)
    await _turn(env.client, parent, "spawn", {"task_queue": task_queue})
    (child,) = (await AgentClient(env.client, parent).get_status()).subagents
    await env.client.get_workflow_handle(child.workflow_id).terminate("gone before its parent")

    events = await _turn(env.client, parent, "stop_all", {"text": ""})
    assert _reply(events) == {"text": "1"}
    stopped = [e.event for e in events if e.event.type == AgentEventType.SUBAGENT_STOPPED]
    assert [s.workflow_id for s in stopped] == [child.workflow_id]
    assert (await AgentClient(env.client, parent).get_status()).subagents == []


def test_only_an_unprocessed_update_is_worth_retrying():
    from temporalio.client import WorkflowUpdateFailedError

    from temporal_agent_harness.harness.agent_client import is_unprocessed_update

    unprocessed = WorkflowUpdateFailedError(ApplicationError("dropped", type="UnprocessedUpdate"))
    rejected = WorkflowUpdateFailedError(ApplicationError("not yours", type="BoardItemNotHeld"))
    assert is_unprocessed_update(unprocessed)
    assert not is_unprocessed_update(rejected)
