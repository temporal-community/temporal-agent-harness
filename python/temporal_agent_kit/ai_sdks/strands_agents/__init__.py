"""Temporal integration for the Strands Agents SDK."""

from temporal_agent_kit.ai_sdks.strands_agents import workflow
from temporal_agent_kit.ai_sdks.strands_agents._plugin import StrandsPlugin
from temporal_agent_kit.ai_sdks.strands_agents._sandbox_activity import (
    SandboxStreamEvent,
    SandboxWorkflowChain,
    SandboxWorkflowContext,
)
from temporal_agent_kit.ai_sdks.strands_agents._temporal_agent import TemporalAgent
from temporal_agent_kit.ai_sdks.strands_agents._temporal_mcp_client import MCPToolNameConflictError, TemporalMCPClient
from temporal_agent_kit.ai_sdks.strands_agents._temporal_sandbox import TemporalSandbox
from temporal_agent_kit.ai_sdks.strands_agents._worker_env_ref import AllowAllWorkerEnvVars, temporal_worker_env_ref

# The Temporal Agent Kit glue for this SDK; imported last because it imports from this package.
from temporal_agent_kit.ai_sdks.strands_agents._kit import (  # noqa: E402
    StrandsStreamObserver,
    kit_observer_factory,
    kit_stream_route,
    as_strands_tool,
    as_strands_tools,
    kit_strands_agent,
    STRANDS_INTERRUPT_NOT_SUPPORTED,
)

__all__ = [
    "AllowAllWorkerEnvVars",
    "MCPToolNameConflictError",
    "SandboxStreamEvent",
    "StrandsPlugin",
    "SandboxWorkflowChain",
    "SandboxWorkflowContext",
    "TemporalAgent",
    "TemporalMCPClient",
    "TemporalSandbox",
    "temporal_worker_env_ref",
    "workflow",
    "StrandsStreamObserver",
    "kit_observer_factory",
    "kit_stream_route",
    "as_strands_tool",
    "as_strands_tools",
    "kit_strands_agent",
    "STRANDS_INTERRUPT_NOT_SUPPORTED",
]
