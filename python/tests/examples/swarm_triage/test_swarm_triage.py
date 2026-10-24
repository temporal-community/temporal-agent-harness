# ABOUTME: End-to-end tests of the swarm triage example: a small swarm of the example's real
# scripted agents runs a backlog to completion against a local dev server with the swarm search
# attributes registered — every issue ends DONE on the board, and one visibility query lists the
# whole swarm by role — and a gated won't-fix shows up as a member AWAITING_APPROVAL, found by
# querying on the phase, until a human approves it.
#
# Run with: uv run pytest tests/examples/swarm_triage -v

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator

import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient
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
    BoardItemStatus,
    BoardSnapshot,
    swarm_board_workflow_id,
)
from temporal_agent_harness.harness.swarm_attributes import AGENT_ROLE, SWARM_SEARCH_ATTRIBUTES
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.swarm_triage.models import Simulation, TriageReport, TriageRun
from examples.swarm_triage.triage_agents import ALL_AGENTS, TASK_QUEUE

# Fast and failure-prone, so a small run still exercises every path: consults that fail and
# hand the issue back, reviews that send it back for rework, workers retiring and being replaced.
FAST = Simulation(
    think_seconds=(0.05, 0.2),
    consult_rate=0.5,
    review_rate=0.5,
    rework_rate=0.5,
    error_rate=0.3,
    approval_rate=0.0,
)


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(
        plugins=[AgentHarnessPlugin()], search_attributes=SWARM_SEARCH_ATTRIBUTES
    )
    yield env
    await env.shutdown()


@pytest_asyncio.fixture(autouse=True)
async def worker(env: WorkflowEnvironment) -> AsyncIterator[None]:
    # Per test, not per module: a worker runs on the event loop that started it, and each test
    # gets its own loop.
    async with Worker(
        env.client, task_queue=TASK_QUEUE, workflows=ALL_AGENTS, max_concurrent_activities=500
    ):
        yield


async def _triage(client: Client, run: TriageRun) -> tuple[str, asyncio.Task[TriageReport]]:
    """Start a dispatcher and its ``triage`` turn; return its id and a task for the report."""
    handle = await client.start_workflow(
        "TriageDispatcher", AgentConfig(), id=f"triage-{uuid.uuid4()}", task_queue=TASK_QUEUE
    )
    reply: AgentMessageReply = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="triage", payload=run.model_dump(mode="json")),
        result_type=AgentMessageReply,
    )

    async def report() -> TriageReport:
        stream = WorkflowStreamClient.create(client, handle.id)
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
        ):
            envelope: AgentEvent = item.data
            if envelope.message_id != reply.message_id:
                continue
            if envelope.event.type == AgentEventType.MESSAGE_HANDLER_ERROR:
                raise AssertionError(envelope.event.message)
            if envelope.event.type == AgentEventType.MESSAGE_HANDLER_END:
                return TriageReport.model_validate(envelope.event.output)
        raise AssertionError("stream ended before the triage reply")

    return handle.id, asyncio.create_task(report())


async def _members(client: Client, query: str) -> list:
    return [execution async for execution in client.list_workflows(query)]


async def test_a_small_swarm_triages_its_whole_backlog(env):
    run = TriageRun(
        issues=24,
        teams=2,
        workers_per_team=3,
        items_per_worker=2,
        specialists=2,
        reviewers=2,
        simulation=FAST,
    )
    dispatcher, report_task = await _triage(env.client, run)
    report = await asyncio.wait_for(report_task, timeout=120)

    assert report.swarm_id == dispatcher
    assert report.done == run.issues
    # Each slot starts a worker per two issues and one more that finds none left, so retired
    # workers were replaced; failed consults handed issues back for another try.
    fixed = run.specialists + run.reviewers + run.teams
    assert report.agents_started > fixed + run.teams * run.workers_per_team
    assert report.consults > 0 and report.reviews > 0

    # One lane per team. A team that drew no issues still has one: its workers' claims made it.
    teams = ["auth", "billing"]
    snapshots = [
        await env.client.get_workflow_handle(swarm_board_workflow_id(dispatcher, team)).query(
            BOARD_SNAPSHOT_QUERY, result_type=BoardSnapshot
        )
        for team in teams
    ]
    assert [s.lane for s in snapshots] == teams
    assert sum(s.counts[BoardItemStatus.DONE] for s in snapshots) == run.issues
    items = [item for s in snapshots for item in s.items]
    assert all(item.tags == [snapshot.lane] for snapshot in snapshots for item in snapshot.items)
    assert sum(item.attempts for item in items) == run.issues + report.releases

    # One query lists the swarm: every agent the run started, the dispatcher and each lane.
    async def listed() -> Counter:
        roles: Counter = Counter()
        for _ in range(50):
            members = await _members(env.client, f"SwarmId = '{dispatcher}'")
            if len(members) == report.agents_started + 1 + run.teams:
                break
            await asyncio.sleep(0.2)
        for member in members:
            roles[member.typed_search_attributes.get(AGENT_ROLE)] += 1
        return roles

    roles = await listed()
    assert roles["dispatcher"] == 1 and roles["board"] == run.teams
    assert roles["lead"] == run.teams
    assert roles["specialist"] == run.specialists and roles["reviewer"] == run.reviewers
    assert roles["worker"] == report.agents_started - run.teams - run.specialists - run.reviewers


async def test_a_wont_fix_waits_for_a_human_and_is_found_by_phase(env):
    run = TriageRun(
        issues=1,
        teams=1,
        workers_per_team=1,
        specialists=1,
        reviewers=1,
        simulation=FAST.model_copy(
            update={"approval_rate": 1.0, "consult_rate": 0.0, "review_rate": 0.0}
        ),
    )
    dispatcher, report_task = await _triage(env.client, run)

    waiting: list = []
    for _ in range(100):
        waiting = await _members(
            env.client, f"SwarmId = '{dispatcher}' AND AgentPhase = 'awaiting_approval'"
        )
        if waiting:
            break
        await asyncio.sleep(0.2)
    assert [m.typed_search_attributes.get(AGENT_ROLE) for m in waiting] == ["worker"]

    worker = AgentClient(env.client, waiting[0].id)
    (pending,) = await worker.get_pending_approvals()
    await worker.approve_tool(pending.tool_id, approved=True)

    report = await asyncio.wait_for(report_task, timeout=60)
    assert (report.done, report.wont_fix) == (1, 1)

