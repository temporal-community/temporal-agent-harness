# ABOUTME: Tests for the swarm endpoints — a session's swarm membership, and a swarm's topology
# (members, parent / peer / board edges, lane counts) read from visibility — against a small run
# of the swarm triage example paused on a won't-fix approval, so every kind of member is alive.
#
# Run with: uv run pytest tests/web/test_swarm_routes.py -v

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
    swarm_board_workflow_id,
)
from temporal_agent_harness.harness.swarm_attributes import SWARM_SEARCH_ATTRIBUTES
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.web.app import create_agent_harness_app
from temporal_agent_harness.web.session_manager import AgentRegistry

from examples.swarm_triage.models import Simulation, TriageRun
from examples.swarm_triage.triage_agents import ALL_AGENTS, TASK_QUEUE


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(
        plugins=[AgentHarnessPlugin()], search_attributes=SWARM_SEARCH_ATTRIBUTES
    )
    yield env
    await env.shutdown()


@pytest_asyncio.fixture
async def http(env: WorkflowEnvironment) -> AsyncIterator[httpx.AsyncClient]:
    app = create_agent_harness_app(registry=AgentRegistry(agents=[]))
    app.state.temporal = env.client
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=ALL_AGENTS):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


async def _paused_run(env: WorkflowEnvironment) -> str:
    """A one-issue triage whose only worker is waiting on a won't-fix approval."""
    run = TriageRun(
        issues=1,
        teams=1,
        workers_per_team=1,
        specialists=1,
        reviewers=1,
        simulation=Simulation(
            think_seconds=(0.0, 0.05), consult_rate=0, review_rate=0, approval_rate=1
        ),
    )
    handle = await env.client.start_workflow(
        "TriageDispatcher", AgentConfig(), id=f"triage-{uuid.uuid4()}", task_queue=TASK_QUEUE
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="triage", payload=run.model_dump(mode="json")),
        result_type=AgentMessageReply,
    )
    return handle.id


async def _topology(http: httpx.AsyncClient, swarm_id: str, until, **params) -> dict:
    """Poll the topology until ``until(topology)`` holds (visibility lags slightly)."""
    for _ in range(100):
        response = await http.get(f"/api/swarms/{swarm_id}/topology", params=params)
        assert response.status_code == 200
        topology = response.json()
        if until(topology):
            return topology
        await asyncio.sleep(0.2)
    raise AssertionError(f"topology never settled: {topology}")


async def test_a_session_reports_its_swarm(env, http):
    dispatcher = await _paused_run(env)
    response = await http.get(f"/api/sessions/{dispatcher}/swarm")
    assert response.json() == {
        "workflow_id": dispatcher,
        "swarm_id": dispatcher,
        "role": "dispatcher",
    }
    missing = await http.get("/api/sessions/no-such-session/swarm")
    assert missing.status_code == 404 and missing.json()["error"] == "workflow_not_found"


async def test_the_topology_lists_every_member_and_edge(env, http):
    dispatcher = await _paused_run(env)

    def paused(t: dict) -> bool:
        return t["phases"].get("awaiting_approval") == 1 and len(t["nodes"]) == 6

    topology = await _topology(http, dispatcher, paused, board="true")
    by_role = {n["role"]: n for n in topology["nodes"]}
    assert sorted(by_role) == ["board", "dispatcher", "lead", "reviewer", "specialist", "worker"]
    assert topology["roles"] == {role: 1 for role in by_role}
    worker, lead = by_role["worker"], by_role["lead"]
    assert worker["phase"] == "awaiting_approval" and not worker["closed"]
    assert worker["parent_id"] == lead["workflow_id"]
    assert by_role["board"]["phase"] is None

    edges = {(e["source"], e["target"], e["kind"]) for e in topology["edges"]}
    assert (dispatcher, lead["workflow_id"], "parent") in edges
    assert (lead["workflow_id"], worker["workflow_id"], "parent") in edges
    lane = swarm_board_workflow_id(dispatcher, "auth")
    assert (worker["workflow_id"], lane, "board") in edges
    assert topology["lanes"] == [
        {"workflow_id": lane, "lane": "auth", "counts": {"open": 0, "claimed": 1, "done": 0}}
    ]

    # Without board=true there are no lane snapshots and no board edges.
    plain = (await http.get(f"/api/swarms/{dispatcher}/topology")).json()
    assert plain["lanes"] == [] and all(e["kind"] != "board" for e in plain["edges"])

    # Approve the won't-fix: the run finishes, and its members read as closed.
    client = AgentClient(env.client, worker["workflow_id"])
    (pending,) = await client.get_pending_approvals()
    await client.approve_tool(pending.tool_id, approved=True)

    def finished(t: dict) -> bool:
        return all(n["closed"] for n in t["nodes"] if n["role"] not in ("board", "dispatcher"))

    done = await _topology(http, dispatcher, finished)
    assert {n["role"] for n in done["nodes"] if not n["closed"]} == {"board", "dispatcher"}
    assert done["phases"] == {"none": 1, "idle": 1}


async def test_an_unknown_swarm_has_no_members(http):
    topology = (await http.get("/api/swarms/no-such-swarm/topology")).json()
    assert (topology["nodes"], topology["edges"], topology["truncated"]) == ([], [], False)
