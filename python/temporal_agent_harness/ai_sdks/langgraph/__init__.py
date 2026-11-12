"""LangGraph plugin for Temporal SDK.

.. warning::
    This package is experimental and may change in future versions.
    Use with caution in production environments.

This plugin runs `LangGraph <https://github.com/langchain-ai/langgraph>`_ nodes
and tasks as Temporal Activities, giving your AI agent workflows durable
execution, automatic retries, and timeouts. It supports both the LangGraph Graph
API (``StateGraph``) and Functional API (``@entrypoint`` / ``@task``).
"""

from temporal_agent_harness.ai_sdks.langgraph._plugin import (
    LangGraphPlugin,
    cache,
)

# Harness: graph() and entrypoint() take an agent's runner; the plugin's seams take the harness's
# tool-call gate and stream observer. The plugin modules never import _harness.
from temporal_agent_harness.ai_sdks.langgraph._harness import (
    LANGGRAPH_INTERRUPT_NOT_SUPPORTED,
    entrypoint,
    graph,
    harness_observer_factory,
    harness_tool_call,
)

__all__ = [
    "LANGGRAPH_INTERRUPT_NOT_SUPPORTED",
    "LangGraphPlugin",
    "cache",
    "entrypoint",
    "graph",
    "harness_observer_factory",
    "harness_tool_call",
]
