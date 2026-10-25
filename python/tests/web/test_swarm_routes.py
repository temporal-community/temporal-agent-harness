# ABOUTME: Tests for the swarm endpoints — a session's swarm membership, and a swarm's topology
# (members, parent / peer / board edges, phase and role counts, lane counts) read from visibility
# — against a small swarm of the harness's test agents in every relationship at once: a lead and
# the worker it started, the worker claiming from the board and then waiting on a peer, and a
# reviewer waiting on an approval.
#
# Run with: uv run pytest tests/web/test_swarm_routes.py -v

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
    SwarmMembership,
    swarm_board_workflow_id,
)
from temporal_agent_harness.harness.swarm_attributes import SWARM_SEARCH_ATTRIBUTES
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.web.app import create_agent_harness_app
from temporal_agent_harness.web.session_manager import AgentRegistry

from tests.harness._swarm_agents import SwarmTestAgent


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(
        plugins=[AgentHarnessPlugin()], search_attributes=SWARM_SEARCH_ATTRIBUTES
    )
    yield env
    await env.shutdown()


@pytest_asyncio.fixture
async def task_queue(env: WorkflowEnvironment) -> AsyncIterator[str]:
    queue = f"swarm-routes-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, workflows=[SwarmTestAgent]):
        yield queue


@pytest_asyncio.fixture
async def http(env: WorkflowEnvironment) -> AsyncIterator[httpx.AsyncClient]:
    app = create_agent_harness_app(registry=AgentRegistry(agents=[]))
    app.state.temporal = env.client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _start(client: Client, task_queue: str, swarm: SwarmMembership) -> str:
    handle = await client.start_workflow(
        "SwarmTestAgent",
        AgentConfig(swarm=swarm),
        id=f"agent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    return handle.id


async def _send(client: Client, workflow_id: str, handler: str, payload: dict) -> None:
    await client.get_workflow_handle(workflow_id).execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=handler, payload=payload),
        result_type=AgentMessageReply,
    )


async def _idle(client: Client, workflow_id: str) -> None:
    """Wait for the agent's open turn to end (its handlers refuse a message mid-turn)."""
    for _ in range(100):
        if not (await AgentClient(client, workflow_id).get_status()).turn_active:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{workflow_id} never went idle")


async def _topology(
    http: httpx.AsyncClient, swarm_id: str, until: Callable[[dict], bool], **params
) -> dict:
    """Poll the topology until ``until(topology)`` holds (visibility lags slightly)."""
    topology: dict = {}
    for _ in range(100):
        response = await http.get(f"/api/swarms/{swarm_id}/topology", params=params)
        assert response.status_code == 200
        topology = response.json()
        if until(topology):
            return topology
        await asyncio.sleep(0.1)
    raise AssertionError(f"topology never settled: {topology}")


def _edges(topology: dict) -> set[tuple[str, str, str]]:
    return {(e["source"], e["target"], e["kind"]) for e in topology["edges"]}


async def _first_subagent(client: Client, parent: str) -> str:
    for _ in range(50):
        subagents = (await AgentClient(client, parent).get_status()).subagents
        if subagents:
            return subagents[0].workflow_id
        await asyncio.sleep(0.1)
    raise AssertionError("the subagent never started")


async def test_a_session_reports_its_swarm(env, task_queue, http):
    swarm = SwarmMembership(swarm_id=f"s-{uuid.uuid4().hex[:8]}", role="lead")
    lead = await _start(env.client, task_queue, swarm)
    # A top-level member publishes its membership on its first workflow task.
    expected = {"workflow_id": lead, "swarm_id": swarm.swarm_id, "role": "lead"}
    for _ in range(50):
        response = await http.get(f"/api/sessions/{lead}/swarm")
        if response.json() == expected:
            break
        await asyncio.sleep(0.1)
    assert response.json() == expected
    missing = await http.get("/api/sessions/no-such-session/swarm")
    assert missing.status_code == 404 and missing.json()["error"] == "workflow_not_found"


async def test_the_topology_lists_every_member_and_edge(env, task_queue, http):
    swarm_id = f"s-{uuid.uuid4().hex[:8]}"
    lead = await _start(env.client, task_queue, SwarmMembership(swarm_id=swarm_id, role="lead"))
    peer = await _start(env.client, task_queue, SwarmMembership(swarm_id=swarm_id, role="peer"))
    reviewer = await _start(
        env.client, task_queue, SwarmMembership(swarm_id=swarm_id, role="reviewer")
    )
    await _send(env.client, lead, "spawn", {"task_queue": task_queue})
    worker = await _first_subagent(env.client, lead)
    # The worker claims from the board (completing one item), then waits on its peer; the
    # reviewer waits on an approval.
    await _send(env.client, worker, "work_board", {"titles": ["a", "b"]})
    await _idle(env.client, worker)
    await _send(env.client, reviewer, "gated", {"text": "hold"})
    await _send(env.client, worker, "ask", {"peer": peer, "handler": "slow_echo", "text": "hi"})

    lane = swarm_board_workflow_id(swarm_id)
    topology = await _topology(
        http,
        swarm_id,
        # The edge is published as the worker starts waiting, which can be before the peer has
        # opened its turn; wait for both.
        lambda t: (worker, peer, "peer") in _edges(t)
        and t["phases"].get("working") == 1
        and t["phases"].get("awaiting_approval") == 1
        and (worker, lane, "board") in _edges(t),
        board="true",
    )
    by_id = {n["workflow_id"]: n for n in topology["nodes"]}
    assert set(by_id) == {lead, worker, peer, reviewer, lane}
    assert topology["roles"] == {"lead": 1, "worker": 1, "peer": 1, "reviewer": 1, "board": 1}
    assert by_id[worker]["parent_id"] == lead and by_id[worker]["phase"] == "awaiting_agent"
    assert by_id[peer]["phase"] == "working"
    assert by_id[reviewer]["phase"] == "awaiting_approval"
    assert by_id[lane]["phase"] is None and not by_id[lane]["closed"]
    assert (lead, worker, "parent") in _edges(topology)
    assert topology["lanes"] == [
        {"workflow_id": lane, "lane": "main", "counts": {"open": 1, "claimed": 0, "done": 1}}
    ]

    # Without board=true there are no lane snapshots and no board edges.
    plain = (await http.get(f"/api/swarms/{swarm_id}/topology")).json()
    assert plain["lanes"] == [] and all(e["kind"] != "board" for e in plain["edges"])

    # Once the approval is answered and the peer replies, the edge goes and phases settle.
    await AgentClient(env.client, reviewer).approve_tool("gated-1", approved=True)
    settled = await _topology(
        http,
        swarm_id,
        lambda t: all(e["kind"] != "peer" for e in t["edges"]) and t["phases"].get("idle") == 4,
    )
    assert settled["phases"] == {"idle": 4, "none": 1}


async def test_any_member_opens_as_a_session(env, task_queue, http):
    swarm_id = f"s-{uuid.uuid4().hex[:8]}"
    lead = await _start(env.client, task_queue, SwarmMembership(swarm_id=swarm_id, role="lead"))
    await _send(env.client, lead, "spawn", {"task_queue": task_queue})
    worker = await _first_subagent(env.client, lead)

    session = (await http.get(f"/api/sessions/{worker}")).json()
    assert session["workflow_id"] == worker
    assert session["agent_workflow_type"] == "SwarmTestAgent"
    assert session["label"] == "worker · SwarmTestAgent"
    assert session["is_discovered"] is True and session["closed"] is False
    missing = await http.get("/api/sessions/no-such-session")
    assert missing.status_code == 404 and missing.json()["error"] == "workflow_not_found"


async def test_an_unknown_swarm_has_no_members(http):
    topology = (await http.get("/api/swarms/no-such-swarm/topology")).json()
    assert (topology["nodes"], topology["edges"], topology["truncated"]) == ([], [], False)
