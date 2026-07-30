"""Implementation of the Nexus-transport MCP server in the shape that OpenAI Agents 
SDK expects.

Wraps `nexus_mcp`'s `WorkflowTransport` to satisfy `agents.mcp.MCPServer`'s ABC contracts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from agents.mcp import MCPServer
from temporalio import workflow

from temporal_agent_harness.ai_sdks.openai_agents._mcp import _DurableMCPServerMarker

if TYPE_CHECKING:
    from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult
    from mcp.types import Tool as MCPTool

_INSTALL_MESSAGE = (
    "Nexus-transport MCP support requires the optional `nexus-mcp` extra, which is only "
    "resolvable from an editable checkout of this repo (it path-depends on "
    "nexus/mcp) and requires Python >=3.13. "
    "Install it with `uv sync --extra nexus-mcp`."
)

try:
    with workflow.unsafe.imports_passed_through():
        from transport.workflow_transport import WorkflowTransport
except ModuleNotFoundError as exc:
    raise RuntimeError(_INSTALL_MESSAGE) from exc


class _NexusTransportMCPServer(_DurableMCPServerMarker, MCPServer):  # type: ignore[misc]
    """MCP server backed by `nexus_mcp`'s `WorkflowTransport` - see module docstring."""

    def __init__(
        self,
        registered_servers: Mapping[str, str],
        name: str | None = None,
        allowed_servers: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> None:
        MCPServer.__init__(self, **kwargs)
        self._transport = WorkflowTransport(
            registered_servers,
            name=name or "nexus-transport",
            allowed_servers=allowed_servers,
        )

    @property
    def name(self) -> str:
        return self._transport.name

    async def connect(self) -> None:
        """
        Nothing to connect since we can use the transport directly,
        see list_tools(...) and call_tool(...) implementations below.
        """

    async def cleanup(self) -> None:
        """Nothing to clean up - see `connect()`."""

    async def __aenter__(self) -> _NexusTransportMCPServer:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.cleanup()

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[MCPTool]:
        return await self._transport.list_tools()

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        return await self._transport.call_tool(tool_name, arguments, meta)

    async def list_prompts(self) -> ListPromptsResult:
        """Nexus-native servers and the Durable Tools Gateway only expose tools for now."""
        # TODO: if we ever implement a Nexus-native prompt registry, we can implement this method.
        from mcp import types

        return types.ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError(
            f"MCP server {self.name!r} (Nexus transport) does not support prompts."
        )