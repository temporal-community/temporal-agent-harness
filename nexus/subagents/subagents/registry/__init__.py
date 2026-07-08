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
    heartbeat_agent_with_registry,
    heartbeat_loop,
    register_agent_with_registry,
)
from subagents.registry.toolset import SCHEMA_ATTR, as_tool, discover_and_build_tools, tool_declaration

__all__ = [
    "SCHEMA_ATTR",
    "AgentElement",
    "AgentRegistryService",
    "DiscoverAgentsInput",
    "HandlerElement",
    "as_tool",
    "deregister_agent_from_registry",
    "discover_and_build_tools",
    "discover_registry_agents",
    "heartbeat_agent_with_registry",
    "heartbeat_loop",
    "register_agent_with_registry",
    "start_subagent_from_registry",
    "tool_declaration",
]
