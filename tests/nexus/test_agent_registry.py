# ABOUTME: Unit tests for AgentRegistryWorkflow's signal/query logic — register, re-register
# (overwrite), deregister (idempotent), discover, and TTL-based staleness (heartbeat_agent +
# list_agents excluding entries not seen within STALE_AFTER_SECONDS). Runs against a real
# (time-skipping) Temporal test environment since it's exercising real workflow signal/query
# dispatch and workflow-clock-driven staleness, but needs no Nexus endpoint — the Nexus service
# handler (registry_service_handler.py) is a thin, untested-here wrapper around exactly this
# signal/query surface.

from __future__ import annotations

import time
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
from agent_registry.registry_workflow import STALE_AFTER_SECONDS


@pytest_asyncio.fixture
async def registry_env():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"agent-registry-test-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=task_queue, workflows=[AgentRegistryWorkflow]):
        handle = await env.client.start_workflow(
            AgentRegistryWorkflow.run,
            id=f"agent-registry-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        yield env, handle


@pytest_asyncio.fixture
async def registry_handle(registry_env):
    _, handle = registry_env
    yield handle


async def test_register_then_discover(registry_handle):
    entry = AgentElement(
        agent_key="qa",
        endpoint="nexus-agent-endpoint",
        handlers=[HandlerElement(name="ask", description="d", parameters={}, output={})],
        description="QA agent",
    )
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry)

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents, time.time())

    assert agents == [entry]


async def test_reregister_overwrites_same_key(registry_handle):
    entry1 = AgentElement(agent_key="qa", endpoint="ep1", handlers=[])
    entry2 = AgentElement(agent_key="qa", endpoint="ep2", handlers=[])
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry1)
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry2)

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents, time.time())

    assert agents == [entry2]


async def test_deregister_removes_entry(registry_handle):
    entry = AgentElement(agent_key="qa", endpoint="ep", handlers=[])
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, entry)
    await registry_handle.signal(AgentRegistryWorkflow.deregister_agent, "qa")

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents, time.time())

    assert agents == []


async def test_deregister_unknown_key_is_noop(registry_handle):
    await registry_handle.signal(AgentRegistryWorkflow.deregister_agent, "does-not-exist")

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents, time.time())

    assert agents == []


async def test_discover_returns_multiple_agents_in_registration_order(registry_handle):
    qa = AgentElement(agent_key="qa", endpoint="ep-qa", handlers=[])
    billing = AgentElement(agent_key="billing", endpoint="ep-billing", handlers=[])
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, qa)
    await registry_handle.signal(AgentRegistryWorkflow.register_agent, billing)

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents, time.time())

    assert agents == [qa, billing]


async def test_entry_ages_out_after_stale_after_seconds(registry_env):
    # sim_now tracks the time-skipping env's clock (real_start + cumulative sleep), not
    # time.time() directly — that's what workflow.now()/_last_seen actually reflect.
    sim_now = time.time()
    env, handle = registry_env
    entry = AgentElement(agent_key="qa", endpoint="ep", handlers=[])
    await handle.signal(AgentRegistryWorkflow.register_agent, entry)

    await env.sleep(STALE_AFTER_SECONDS + 1)
    sim_now += STALE_AFTER_SECONDS + 1

    agents = await handle.query(AgentRegistryWorkflow.list_agents, sim_now)
    assert agents == []


async def test_heartbeat_keeps_entry_alive_past_original_stale_window(registry_env):
    sim_now = time.time()
    env, handle = registry_env
    entry = AgentElement(agent_key="qa", endpoint="ep", handlers=[])
    await handle.signal(AgentRegistryWorkflow.register_agent, entry)

    # Halfway through the staleness window, a heartbeat resets the clock.
    await env.sleep(STALE_AFTER_SECONDS / 2)
    sim_now += STALE_AFTER_SECONDS / 2
    await handle.signal(AgentRegistryWorkflow.heartbeat_agent, "qa")
    await env.sleep(STALE_AFTER_SECONDS / 2 + 1)
    sim_now += STALE_AFTER_SECONDS / 2 + 1

    agents = await handle.query(AgentRegistryWorkflow.list_agents, sim_now)
    assert agents == [entry]


async def test_heartbeat_unknown_key_is_noop(registry_handle):
    await registry_handle.signal(AgentRegistryWorkflow.heartbeat_agent, "does-not-exist")

    agents = await registry_handle.query(AgentRegistryWorkflow.list_agents, time.time())
    assert agents == []


async def test_stale_entry_does_not_block_other_agents(registry_env):
    sim_now = time.time()
    env, handle = registry_env
    stale = AgentElement(agent_key="stale", endpoint="ep-stale", handlers=[])
    await handle.signal(AgentRegistryWorkflow.register_agent, stale)

    await env.sleep(STALE_AFTER_SECONDS + 1)
    sim_now += STALE_AFTER_SECONDS + 1

    fresh = AgentElement(agent_key="fresh", endpoint="ep-fresh", handlers=[])
    await handle.signal(AgentRegistryWorkflow.register_agent, fresh)

    agents = await handle.query(AgentRegistryWorkflow.list_agents, sim_now)
    assert agents == [fresh]
