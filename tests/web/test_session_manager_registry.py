"""The session manager takes a new agent registry without being restarted."""

from __future__ import annotations

import uuid

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.web.app import _ensure_session_manager_workflow
from temporal_agent_harness.web.session_manager import (
    AgentDescriptor,
    AgentRegistry,
    SessionManagerWorkflow,
)


def _registry(*keys: str) -> AgentRegistry:
    return AgentRegistry(
        agents=[
            AgentDescriptor(key=k, workflow_type=f"{k}Agent", task_queue="q", label=k, description=k)
            for k in keys
        ]
    )


async def test_a_restarted_server_pushes_its_registry_to_the_running_manager():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    queue = f"manager-{uuid.uuid4()}"
    try:
        async with Worker(env.client, task_queue=queue, workflows=[SessionManagerWorkflow]):
            manager_id = f"session-manager-{uuid.uuid4()}"
            handle = await _ensure_session_manager_workflow(
                env.client, registry=_registry("a"), manager_workflow_id=manager_id, manager_task_queue=queue
            )
            first = await handle.query(SessionManagerWorkflow.available_agents)
            assert [a.key for a in first.agents] == ["a"]

            # The server restarts with an agent added: the same manager now lists both.
            handle = await _ensure_session_manager_workflow(
                env.client, registry=_registry("a", "b"), manager_workflow_id=manager_id, manager_task_queue=queue
            )
            second = await handle.query(SessionManagerWorkflow.available_agents)
            assert [a.key for a in second.agents] == ["a", "b"]
    finally:
        await env.shutdown()
