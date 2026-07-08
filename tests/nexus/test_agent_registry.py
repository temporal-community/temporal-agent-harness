# ABOUTME: Unit tests for AgentRegistryWorkflow's signal/query logic — register, re-register
# (overwrite), deregister (idempotent), and discover. Runs against a real (time-skipping) Temporal
# test environment since it's exercising real workflow signal/query dispatch, but needs no Nexus
# endpoint — the Nexus service handler (registry_service_handler.py) is a thin, untested-here
# wrapper around exactly this signal/query surface.

from __future__ import annotations

import uuid

import pytest_asyncio
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agent_registry import (
    AgentElement,
    AgentRegistryWorkflow,
    HandlerElement,
)


@pytest_asyncio.fixture
async def registry_handle():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"agent-registry-test-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=task_queue, workflows=[AgentRegistryWorkflow]):
        handle = await env.client.start_workflow(
            AgentRegistryWorkflow.run,
            id=f"agent-registry-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        yield handle


async def test_register_then_discover(registry_handle):
    entry = AgentElement(
        agent_key="qa",
        endpoint="nexus-agent-endpoint",
        handlers=[HandlerElement(name="ask", description="d", parameters={}, output={})],
        description="QA agent",
    )
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry)

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents)

    assert agents == [entry]


async def test_reregister_overwrites_same_key(registry_handle):
    entry1 = AgentElement(agent_key="qa", endpoint="ep1", handlers=[])
    entry2 = AgentElement(agent_key="qa", endpoint="ep2", handlers=[])
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry1)
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry2)

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents)

    assert agents == [entry2]


async def test_deregister_removes_entry(registry_handle):
    entry = AgentElement(agent_key="qa", endpoint="ep", handlers=[])
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry)
    await registry_handle.signal(AgentRegistryWorkflow.deregister_agent, "qa")

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents)

    assert agents == []


async def test_deregister_unknown_key_is_noop(registry_handle):
    await registry_handle.signal(AgentRegistryWorkflow.deregister_agent, "does-not-exist")

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents)

    assert agents == []


async def test_discover_returns_multiple_agents_in_registration_order(registry_handle):
    qa = AgentElement(agent_key="qa", endpoint="ep-qa", handlers=[])
    billing = AgentElement(agent_key="billing", endpoint="ep-billing", handlers=[])
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, qa)
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, billing)

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents)

    assert agents == [qa, billing]
