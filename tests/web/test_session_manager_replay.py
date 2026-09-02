# ABOUTME: The session manager is the one workflow here that is meant to outlive every server and
# every deploy, so its history is replayed by whatever worker picks it up next. This pins the two
# things a cutover depends on: every update it has ever accepted still has a handler, and its child
# starts record ABANDON — which the replayer does NOT check, and which decides whether restarting
# the manager takes every live session down with it.

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporal_agent_harness.web.session_manager import (
    AgentDescriptor,
    AgentRegistry,
    CreateSessionRequest,
    SetSessionsArchivedRequest,
    SessionManagerWorkflow,
)

# The update names the live manager's 578-event history actually contains. Replaying it needs a
# handler for each; before set_available_agents existed here, replay died on event 48 with
# "No command scheduled for event HistoryEvent(id: 48, WorkflowExecutionUpdateAccepted)".
LIVE_HISTORY_UPDATES = ("create_session", "set_available_agents", "set_sessions_archived")


@workflow.defn(name="StubAgent")
class StubAgent:
    """Stands in for a real agent: the manager only ever awaits the child's START."""

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await workflow.wait_condition(lambda: False)


def _registry(task_queue: str) -> AgentRegistry:
    return AgentRegistry(
        agents=[
            AgentDescriptor(
                key="stub",
                workflow_type="StubAgent",
                task_queue=task_queue,
                label="Stub",
                description="",
            )
        ]
    )


@pytest_asyncio.fixture
async def env_and_client():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"manager-replay-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SessionManagerWorkflow, StubAgent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _manager_with_every_update(client: Client, task_queue: str):
    """Drive one manager through every update the live history contains."""
    handle = await client.start_workflow(
        SessionManagerWorkflow.run,
        _registry(task_queue),
        id=f"session-manager-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    session = await handle.execute_update(
        SessionManagerWorkflow.create_session,
        CreateSessionRequest(agent_workflow_type="StubAgent", config=AgentConfig()),
    )
    await handle.execute_update(
        SessionManagerWorkflow.set_available_agents, _registry(task_queue)
    )
    await handle.execute_update(
        SessionManagerWorkflow.set_sessions_archived,
        SetSessionsArchivedRequest(workflow_ids=[session.workflow_id]),
    )
    return handle


@pytest.mark.asyncio
async def test_the_manager_replays_a_history_of_every_update_it_accepts(
    env_and_client,
) -> None:
    client, task_queue = env_and_client
    handle = await _manager_with_every_update(client, task_queue)

    history = await handle.fetch_history()
    accepted = {
        event.workflow_execution_update_accepted_event_attributes.accepted_request.input.name
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED
    }
    assert accepted == set(LIVE_HISTORY_UPDATES), (
        "this history no longer covers what the live manager's does, so replaying it proves less"
    )

    # The real check: a worker running today's code picks this history up and gets through it.
    await Replayer(
        workflows=[SessionManagerWorkflow], data_converter=pydantic_data_converter
    ).replay_workflow(history)


@pytest.mark.asyncio
async def test_sessions_are_started_to_outlive_their_manager(env_and_client) -> None:
    # Asserted on the RECORDED attribute rather than the call, because that is what the server
    # acts on — and because the replayer does not compare it, so a regression here replays
    # perfectly clean while meaning that restarting the manager terminates every live session.
    client, task_queue = env_and_client
    handle = await _manager_with_every_update(client, task_queue)

    history = await handle.fetch_history()
    policies = [
        event.start_child_workflow_execution_initiated_event_attributes.parent_close_policy
        for event in history.events
        if event.event_type
        == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]

    assert policies, "expected the manager to have started a child session"
    assert all(
        policy == workflow.ParentClosePolicy.ABANDON.value for policy in policies
    ), f"sessions must outlive the manager; got {policies}"
