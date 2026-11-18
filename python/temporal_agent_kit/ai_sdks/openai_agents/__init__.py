"""Support for using the OpenAI Agents SDK as part of Temporal workflows.

This module provides compatibility between the
`OpenAI Agents SDK <https://github.com/openai/openai-agents-python>`_ and Temporal workflows.
"""

from temporal_agent_kit.ai_sdks.openai_agents._mcp import (
    StatefulMCPServerProvider,
    StatelessMCPServerProvider,
)
from temporal_agent_kit.ai_sdks.openai_agents._model_parameters import ModelActivityParameters
from temporal_agent_kit.ai_sdks.openai_agents._temporal_openai_agents import (
    OpenAIAgentsPlugin,
    OpenAIPayloadConverter,
)
from temporal_agent_kit.ai_sdks.openai_agents.sandbox._sandbox_client_provider import (
    SandboxClientProvider,
)
from temporal_agent_kit.ai_sdks.openai_agents.workflow import AgentsWorkflowError

from temporal_agent_kit.ai_sdks.openai_agents import testing, workflow

# The Temporal Agent Kit glue for this SDK; imported last because it imports from this package.
from temporal_agent_kit.ai_sdks.openai_agents._kit import (  # noqa: E402
    OpenAIStreamObserver,
    kit_observer_factory,
    stream_to_provider,
    as_openai_agent_tool,
    as_openai_agent_tools,
    as_kit_mcp_server,
    as_kit_mcp_servers,
    is_kit_mcp_server,
    mark_durable_mcp_server,
    is_durable_mcp_server,
)

__all__ = [
    "AgentsWorkflowError",
    "ModelActivityParameters",
    "OpenAIAgentsPlugin",
    "OpenAIPayloadConverter",
    "SandboxClientProvider",
    "StatelessMCPServerProvider",
    "StatefulMCPServerProvider",
    "testing",
    "workflow",
    "OpenAIStreamObserver",
    "kit_observer_factory",
    "stream_to_provider",
    "as_openai_agent_tool",
    "as_openai_agent_tools",
    "as_kit_mcp_server",
    "as_kit_mcp_servers",
    "is_kit_mcp_server",
    "mark_durable_mcp_server",
    "is_durable_mcp_server",
]
