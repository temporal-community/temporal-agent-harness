"""MCP server backed by Nexus.

Wraps nexus_mcp's WorkflowTransport to satisfy agents.mcp.MCPServer's ABC contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from agents.mcp import MCPServer
from temporalio import workflow

if TYPE_CHECKING:
    from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult
    from mcp.types import Tool as MCPTool

_INSTALL_MESSAGE = (
    "Nexus-brokered MCP support requires the optional `nexus-mcp` extra, which is only "
    "resolvable from an editable checkout of this repo (it path-depends on "
    "nexus/mcp) and requires Python >=3.13. "
    "Install it with `uv sync --extra nexus-mcp`."
)

try:
    with workflow.unsafe.imports_passed_through():
        from transport.workflow_transport import WorkflowTransport, _coerce_call_tool_result
        from durable_tools_gateway.generated import CallToolInput, ListAgentEntriesInput, RegistryService
except ModuleNotFoundError as exc:
    raise RuntimeError(_INSTALL_MESSAGE) from exc


def _error_result(exc: Exception) -> CallToolResult:
    """Wrap a caught exception as an isError=True CallToolResult."""
    from mcp import types

    return types.CallToolResult(content=[types.TextContent(type="text", text=str(exc))], isError=True)


class _BaseNexusMCPServer(MCPServer):  # type: ignore[misc]
    """Shared ABC boilerplate for Nexus-backed MCP servers."""

    async def connect(self) -> None:
        """No-op. list_tools/call_tool below call Nexus directly."""

    async def cleanup(self) -> None:
        """No-op. See connect()."""

    async def __aenter__(self) -> _BaseNexusMCPServer:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.cleanup()

    async def list_prompts(self) -> ListPromptsResult:
        from mcp import types

        return types.ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError(f"MCP server {self.name!r} does not support prompts.")


class _NexusTransportMCPServer(_BaseNexusMCPServer):
    """MCP server backed by nexus_mcp's WorkflowTransport. See module docstring."""

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

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[MCPTool]:
        return await self._transport.list_tools()

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        return await self._transport.call_tool(tool_name, arguments, meta)


class NexusGateway:
    """Handle on the Durable Tools Gateway's 3rd-party servers registered for one
    agent_id. Not an MCPServer -- call .mcp_servers(*aliases) to get one.
    """

    def __init__(
        self,
        agent_id: str,
        gateway_name: str = "RegistryService",
        gateway_endpoint: str = "mcp-registry-endpoint",
    ) -> None:
        self._agent_id = agent_id
        self._gateway_name = gateway_name
        self._gateway_endpoint = gateway_endpoint

    def mcp_servers(self, *aliases: str) -> MCPServer:
        """One MCPServer exposing the given registered aliases' tools, fetched with a
        single Nexus call. An alias that isn't actually registered is silently skipped
        for now (no error handling yet -- this is a prototype).
        """
        display_name = f"{self._agent_id}-{self._gateway_name}-{self._gateway_endpoint}"
        return _NexusGatewayMCPServer(
            self._agent_id,
            frozenset(aliases),
            self._gateway_name,
            self._gateway_endpoint,
            display_name,
        )


class _NexusGatewayMCPServer(_BaseNexusMCPServer):
    """MCP server for a chosen set of 3rd-party aliases registered under one agent_id,
    proxied through the Durable Tools Gateway. Resolved fresh on every list_tools()
    call -- nothing is cached, and nothing is registered here (see
    durable_tools_gateway's register_external for that).
    """

    def __init__(
        self,
        agent_id: str,
        aliases: frozenset[str],
        gateway_name: str,
        gateway_endpoint: str,
        display_name: str,
        **kwargs: Any,
    ) -> None:
        MCPServer.__init__(self, **kwargs)
        self._agent_id = agent_id
        self._aliases = aliases
        self._gateway_name = gateway_name
        self._gateway_endpoint = gateway_endpoint
        self._display_name = display_name
        self._remote_routes: dict[str, str] = {}  # tool name -> alias

    @property
    def name(self) -> str:
        return self._display_name

    async def list_tools(self, run_context: Any = None, agent: Any = None) -> list[MCPTool]:
        from mcp import types

        gateway_client = workflow.create_nexus_client(
            service=self._gateway_name, endpoint=self._gateway_endpoint
        )
        entries = await gateway_client.execute_operation(
            RegistryService.list_agent_entries, ListAgentEntriesInput(agent_id=self._agent_id)
        )
        remote_tools_by_alias = entries.remote_tools or {}

        remote_routes: dict[str, str] = {}
        tool_dicts: list[dict[str, Any]] = []
        for alias in self._aliases:
            for tool_dict in remote_tools_by_alias.get(alias, []):
                remote_routes[tool_dict["name"]] = alias
                tool_dicts.append(tool_dict)

        self._remote_routes = remote_routes
        return [types.Tool(**d) for d in tool_dicts]

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> CallToolResult:
        from mcp import types

        alias = self._remote_routes.get(tool_name)
        if alias is None:
            return types.CallToolResult(
                content=[types.TextContent(
                    type="text", text=f"Unknown tool {tool_name!r} for agent {self._agent_id!r}."
                )],
                isError=True,
            )

        try:
            gateway_client = workflow.create_nexus_client(
                service=self._gateway_name, endpoint=self._gateway_endpoint
            )
            call_result = await gateway_client.execute_operation(
                RegistryService.call_tool,
                CallToolInput(
                    agent_id=self._agent_id, alias=alias, name=tool_name, arguments=arguments or {}
                ),
            )
            return _coerce_call_tool_result(call_result.result)
        except Exception as exc:
            return _error_result(exc)
