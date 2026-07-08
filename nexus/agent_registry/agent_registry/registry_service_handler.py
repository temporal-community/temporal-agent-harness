# ABOUTME: AgentRegistryServiceHandler - implementation of AgentRegistryService operations.
# Mirrors nexus_mcp/registry/registry_service_handler.py: each operation is a thin Nexus-facing
# wrapper that signals or queries the long-lived AgentRegistryWorkflow, which is assumed to
# already be running (started once by this registry's own worker at startup — see worker.py).

from __future__ import annotations

import nexusrpc.handler
from nexusrpc.handler import StartOperationContext
from temporalio.client import Client

from agent_registry.agent_registry_service import (
    AgentElement,
    AgentRegistryService,
    DeregisterAgentInput,
    DeregisterAgentOutput,
    DiscoverAgentsInput,
    DiscoverAgentsOutput,
    RegisterAgentInput,
    RegisterAgentOutput,
)
from agent_registry.registry_workflow import (
    AGENT_REGISTRY_WORKFLOW_ID,
    AgentRegistryWorkflow,
)


@nexusrpc.handler.service_handler(service=AgentRegistryService)
class AgentRegistryServiceHandler:
    """Callers from any namespace reach this handler via the registry's Nexus endpoint —
    a Nexus call rather than a direct workflow signal so registration/discovery work across
    namespace (and cluster) boundaries, same rationale as the tool registry."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @nexusrpc.handler.sync_operation
    async def register_agent(
        self, ctx: StartOperationContext, input: RegisterAgentInput
    ) -> RegisterAgentOutput:
        handle = self._client.get_workflow_handle(AGENT_REGISTRY_WORKFLOW_ID)
        await handle.signal(
            AgentRegistryWorkflow.register_agent,
            AgentElement(
                agent_key=input.agent_key,
                endpoint=input.endpoint,
                handlers=input.handlers,
                description=input.description,
            ),
        )
        return RegisterAgentOutput(registered=True)

    @nexusrpc.handler.sync_operation
    async def deregister_agent(
        self, ctx: StartOperationContext, input: DeregisterAgentInput
    ) -> DeregisterAgentOutput:
        handle = self._client.get_workflow_handle(AGENT_REGISTRY_WORKFLOW_ID)
        await handle.signal(AgentRegistryWorkflow.deregister_agent, input.agent_key)
        return DeregisterAgentOutput(deregistered=True)

    @nexusrpc.handler.sync_operation
    async def discover_agents(
        self, ctx: StartOperationContext, input: DiscoverAgentsInput
    ) -> DiscoverAgentsOutput:
        handle = self._client.get_workflow_handle(AGENT_REGISTRY_WORKFLOW_ID)
        agents = await handle.query(AgentRegistryWorkflow.list_agents)
        return DiscoverAgentsOutput(agents=agents)
