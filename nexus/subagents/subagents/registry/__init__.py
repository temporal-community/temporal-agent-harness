# ABOUTME: Dynamic subagent discovery, built on subagents.transport's NexusTransport —
# register a harness agent with the agent registry, discover what's registered, and drive a
# discovered agent as a subagent (directly, via NexusTransport; the registry itself is never
# in the turn-driving path).

from subagents.registry.agent_registry_service import (
    AgentElement,
    AgentRegistryService,
    DiscoverAgentsInput,
    HandlerElement,
)
from subagents.registry.discovery import discover_registry_agents, start_subagent_from_registry
from subagents.registry.registration import (
    deregister_agent_from_registry,
    register_agent_with_registry,
)
from subagents.registry.toolset import registry_subagent_toolset

__all__ = [
    "AgentElement",
    "AgentRegistryService",
    "DiscoverAgentsInput",
    "HandlerElement",
    "deregister_agent_from_registry",
    "discover_registry_agents",
    "register_agent_with_registry",
    "registry_subagent_toolset",
    "start_subagent_from_registry",
]
