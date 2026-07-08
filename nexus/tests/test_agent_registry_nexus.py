# ABOUTME: End-to-end test of the agent registry's REAL Nexus wire path: harness-side
# register_agent_with_registry -> a live Nexus endpoint fronting AgentRegistryServiceHandler ->
# discover_agents -> harness-side deregister_agent_from_registry. Complements (does not
# replace) tests/nexus/test_agent_registry.py, which drives AgentRegistryWorkflow directly via
# signal/query and never exercises the Nexus service handler or a real endpoint at all.
#
# See conftest.py's module docstring for the server requirement (standalone Nexus operations,
# not yet in a released Temporal server) and how to build it — this test SKIPS cleanly without
# a matching binary. Tests share ONE registry (agent_registry_endpoint, session-scoped — see
# that fixture's docstring for why) and use distinct agent_keys to stay independent.

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, Field

from temporal_agent_harness.harness import agent
from subagents.registry import (
    AgentRegistryService,
    DiscoverAgentsInput,
    deregister_agent_from_registry,
    register_agent_with_registry,
)

# loop_scope="session" — must match the session-scoped fixtures' loop_scope in conftest.py, or
# the worker's background poller (started on the fixture's loop) silently stalls once control
# returns to a test on a different loop. See conftest.py's module docstring.
pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Question(BaseModel):
    """A question to research."""

    text: str = Field(description="The natural-language question to research.")


class _Answer(BaseModel):
    """An answer to a question."""

    text: str = Field(description="The answer text.")


class _SampleAgent:
    """A minimal agent for this test — subagent_registry only needs its @agent.accepts
    handlers, read by pure reflection (agent_handlers), so no @workflow.defn is required."""

    @agent.accepts
    async def ask(self, q: _Question) -> _Answer:
        """Answer a free-form question."""
        ...


async def test_register_discover_deregister_over_real_nexus(devserver_client, agent_registry_endpoint):
    client = devserver_client
    registry_endpoint = agent_registry_endpoint
    agent_key = f"sample-{uuid.uuid4().hex[:8]}"
    fronted_endpoint = f"sample-agent-endpoint-{agent_key}"  # opaque here — never dialed

    await register_agent_with_registry(
        client,
        _SampleAgent,
        agent_key=agent_key,
        endpoint=fronted_endpoint,
        registry_endpoint=registry_endpoint,
        description="Sample agent for the Nexus wire-path test.",
    )

    registry = client.create_nexus_client(service=AgentRegistryService, endpoint=registry_endpoint)
    result = await registry.execute_operation(
        AgentRegistryService.discover_agents, DiscoverAgentsInput(), id=f"discover-{agent_key}-1"
    )
    [entry] = [a for a in result.agents if a.agent_key == agent_key]
    assert entry.endpoint == fronted_endpoint
    assert [h.name for h in entry.handlers] == ["ask"]

    await deregister_agent_from_registry(client, agent_key=agent_key, registry_endpoint=registry_endpoint)

    result_after = await registry.execute_operation(
        AgentRegistryService.discover_agents, DiscoverAgentsInput(), id=f"discover-{agent_key}-2"
    )
    assert agent_key not in [a.agent_key for a in result_after.agents]


async def test_register_is_idempotent_across_worker_restarts(devserver_client, agent_registry_endpoint):
    """register_agent_with_registry deregisters before registering — a worker restart
    re-announces cleanly instead of erroring on an already-registered key."""
    client = devserver_client
    registry_endpoint = agent_registry_endpoint
    agent_key = f"sample-{uuid.uuid4().hex[:8]}"

    for endpoint in (f"{agent_key}-endpoint-v1", f"{agent_key}-endpoint-v2"):
        await register_agent_with_registry(
            client, _SampleAgent, agent_key=agent_key, endpoint=endpoint, registry_endpoint=registry_endpoint
        )

    registry = client.create_nexus_client(service=AgentRegistryService, endpoint=registry_endpoint)
    result = await registry.execute_operation(
        AgentRegistryService.discover_agents, DiscoverAgentsInput(), id=f"discover-{agent_key}"
    )
    # Only the latest registration survives for this key — not a duplicate entry.
    [entry] = [a for a in result.agents if a.agent_key == agent_key]
    assert entry.endpoint == f"{agent_key}-endpoint-v2"

    await deregister_agent_from_registry(client, agent_key=agent_key, registry_endpoint=registry_endpoint)
