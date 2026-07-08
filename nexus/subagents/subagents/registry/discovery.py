# ABOUTME: Workflow-side discovery helpers — query the agent registry's directory, and start a
# discovered agent as a subagent via NexusTransport. These are plain functions (not methods on
# AgentWorkflowRunner) taking ``runner`` explicitly: everything they need
# (``discover_registry_agents``/``AgentWorkflowRunner.start_subagent``) is already public
# harness surface, so nothing here needs privileged access to the runner's internals.
#
# Unlike registration.py's client-side register/deregister, these run from WORKFLOW code
# (``workflow.create_nexus_client``) and need no standalone-Nexus server capability.

from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

from ..transport import NexusTransport
from .agent_registry_service import AgentElement, AgentRegistryService, DiscoverAgentsInput


async def discover_registry_agents(registry_endpoint: str) -> list[AgentElement]:
    """Query the agent registry's current directory — every registered agent's key,
    endpoint, description, and capability (its ``@agent.accepts`` handlers, tool-style).

    A plain Nexus operation call, deterministic and replay-safe like any other (recorded in
    workflow history same as an activity call) — no caching, always the live directory."""
    nexus_client = workflow.create_nexus_client(
        service=AgentRegistryService, endpoint=registry_endpoint
    )
    discover_handle = await nexus_client.start_operation(
        AgentRegistryService.discover_agents, DiscoverAgentsInput()
    )
    result = await discover_handle
    return result.agents


async def start_subagent_from_registry(
    runner: AgentWorkflowRunner, agent_key: str, registry_endpoint: str
) -> str:
    """Start a subagent by looking up ``agent_key`` in the registry's directory (a fresh
    lookup — not cached from an earlier ``discover_registry_agents`` call, so a stale key
    from an earlier turn fails clearly rather than silently reusing an old endpoint) and
    wiring it via ``runner.start_subagent`` with a freshly-built ``NexusTransport``."""
    agents = await discover_registry_agents(registry_endpoint)
    for entry in agents:
        if entry.agent_key == agent_key:
            return await runner.start_subagent(
                agent_key, transport=NexusTransport(entry.endpoint)
            )
    raise ApplicationError(
        f"Unknown registry agent {agent_key!r}. Currently registered: "
        f"{sorted(a.agent_key for a in agents)}.",
        {"agent_key": agent_key, "known": sorted(a.agent_key for a in agents)},
        type="UnknownRegistryAgent",
        non_retryable=True,
    )
