# ABOUTME: The agent registry — a standalone Nexus service (own workflow + Nexus service
# handler + worker) that agents register their capability with, so a parent agent can discover
# what's available and invoke any of them as a subagent. Talked to purely via Nexus operations —
# see temporal_agent_harness/harness/subagent_registry/ for the harness's own client-side copy
# of this contract. Re-exports the flat public surface.

from agent_registry.agent_registry_service import (
    AgentElement,
    AgentRegistryService,
    DeregisterAgentInput,
    DeregisterAgentOutput,
    DiscoverAgentsInput,
    DiscoverAgentsOutput,
    HandlerElement,
    RegisterAgentInput,
    RegisterAgentOutput,
)
from agent_registry.registry_service_handler import AgentRegistryServiceHandler
from agent_registry.registry_workflow import (
    AGENT_REGISTRY_WORKFLOW_ID,
    AgentRegistryWorkflow,
)

__all__ = [
    "AgentElement",
    "AgentRegistryService",
    "DeregisterAgentInput",
    "DeregisterAgentOutput",
    "DiscoverAgentsInput",
    "DiscoverAgentsOutput",
    "HandlerElement",
    "RegisterAgentInput",
    "RegisterAgentOutput",
    "AgentRegistryServiceHandler",
    "AGENT_REGISTRY_WORKFLOW_ID",
    "AgentRegistryWorkflow",
]
