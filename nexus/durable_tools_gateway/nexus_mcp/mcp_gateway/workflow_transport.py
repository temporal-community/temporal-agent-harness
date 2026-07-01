"""WorkflowTransport — proper MCP transport for Temporal workflows.

Follows the shape of https://github.com/bergundy/nexus-mcp-python but adds
support for both Nexus-backed servers *and* external HTTP servers:

  - ``list_tools`` queries ToolRegistryWorkflow (both kinds share the registry).
  - ``call_tool`` routes directly:
      Nexus-backed  → workflow.create_nexus_client().execute_operation()
                       No ToolCallWorkflow indirection; one fewer Temporal hop.
      External HTTP → mcp_proxy_activity (outbound HTTP from the worker).

Usage inside a Temporal workflow::

    transport = WorkflowTransport(nexus_endpoint="qa-tools-endpoint")
    async with transport.connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools  = await session.list_tools()
            result = await session.call_tool("docsproxy_read_page", {"url": "..."})
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncGenerator

import anyio
import anyio.streams.memory
import mcp.types as types
import pydantic
from mcp.shared.message import SessionMessage
from temporalio import workflow

from registry import REGISTRY_WORKFLOW_ID, RegistryEntry, ToolRegistryWorkflow
from .tool_call import ExternalMCPCallInput, mcp_proxy_activity
from .transport import gateway_list_tools_activity, registry_find_activity


class WorkflowTransport:
    """In-process MCP transport backed by Temporal Nexus and the tool registry.

    Connects to Nexus-backed MCP servers directly (``workflow.create_nexus_client``)
    and to external HTTP servers via ``mcp_proxy_activity`` — no HTTP server or
    public URL required on the caller side.

    Args:
        nexus_endpoint: Temporal Nexus endpoint name used to reach Nexus-backed
                        MCP service handlers (e.g. ``"qa-tools-endpoint"``).
    """

    def __init__(self, nexus_endpoint: str) -> None:
        self._nexus_endpoint = nexus_endpoint

    @asynccontextmanager
    async def connect(
        self,
    ) -> AsyncGenerator[
        tuple[
            anyio.streams.memory.MemoryObjectReceiveStream[SessionMessage],
            anyio.streams.memory.MemoryObjectSendStream[SessionMessage],
        ],
        None,
    ]:
        """Open an in-process MCP transport pair compatible with ``mcp.ClientSession``."""
        client_write, transport_read = anyio.create_memory_object_stream(0)  # type: ignore[var-annotated]
        transport_write, client_read = anyio.create_memory_object_stream(0)  # type: ignore[var-annotated]

        async def _router() -> None:
            try:
                async for session_message in transport_read:
                    request = session_message.message.root
                    if not isinstance(request, types.JSONRPCRequest):
                        continue  # ignore notifications etc.

                    result: types.Result | types.ErrorData
                    try:
                        match request:
                            case types.JSONRPCRequest(method="initialize"):
                                result = self._handle_initialize(
                                    types.InitializeRequestParams.model_validate(request.params)
                                )
                            case types.JSONRPCRequest(method="tools/list"):
                                result = await self._handle_list_tools()
                            case types.JSONRPCRequest(method="tools/call"):
                                result = await self._handle_call_tool(
                                    types.CallToolRequestParams.model_validate(request.params)
                                )
                            case _:
                                result = types.ErrorData(
                                    code=types.METHOD_NOT_FOUND,
                                    message=f"Unknown method: {request.method}",
                                )
                    except pydantic.ValidationError as exc:
                        result = types.ErrorData(
                            code=types.INVALID_PARAMS,
                            message=f"Invalid request params: {exc}",
                        )

                    response = (
                        _json_rpc_result(request, result)
                        if isinstance(result, types.Result)
                        else _json_rpc_error(request, result)
                    )
                    await transport_write.send(SessionMessage(types.JSONRPCMessage(root=response)))

            except anyio.ClosedResourceError:
                pass
            finally:
                await transport_write.aclose()

        router_task = asyncio.create_task(_router())
        try:
            yield client_read, client_write
        finally:
            await client_write.aclose()
            router_task.cancel()
            try:
                await router_task
            except asyncio.CancelledError:
                pass
            await transport_read.aclose()

    # -- MCP method handlers ---------------------------------------------------

    def _handle_initialize(
        self, params: types.InitializeRequestParams
    ) -> types.InitializeResult:
        return types.InitializeResult(
            protocolVersion="2024-11-05",
            capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
            serverInfo=types.Implementation(name="workflow-transport", version="0.1.0"),
        )

    async def _handle_list_tools(self) -> types.ListToolsResult:
        """Return all tools from the registry (Nexus-backed + external)."""
        tool_dicts: list[dict[str, Any]] = await workflow.execute_activity(
            gateway_list_tools_activity,
            start_to_close_timeout=timedelta(seconds=30),
        )
        return types.ListToolsResult(tools=[types.Tool(**d) for d in tool_dicts])

    async def _handle_call_tool(
        self, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """Route the call: direct Nexus for 1st-party, mcp_proxy_activity for 3rd-party."""
        service, _, operation = params.name.partition("_")
        if not service or not operation:
            return types.CallToolResult(
                content=[types.TextContent(type="text",
                    text=f"Invalid tool name {params.name!r}: expected 'service_operation'")],
                isError=True,
            )

        entry: RegistryEntry | None = await workflow.execute_activity(
            registry_find_activity,
            args=[service],
            start_to_close_timeout=timedelta(seconds=10),
        )
        if entry is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text",
                    text=f"Service {service!r} is not registered in the gateway.")],
                isError=True,
            )

        arguments = params.arguments or {}
        try:
            if entry.kind == "nexus":
                # Direct Nexus — bypass ToolCallWorkflow, one fewer Temporal hop.
                nexus_client = workflow.create_nexus_client(
                    service=service,
                    endpoint=entry.endpoint,
                )
                result: Any = await nexus_client.execute_operation(operation, arguments)
                text = (
                    result if isinstance(result, str)
                    else result.model_dump_json(indent=2) if hasattr(result, "model_dump_json")
                    else str(result)
                )
            else:
                # External HTTP — outbound call from the worker via activity.
                text = await workflow.execute_activity(
                    mcp_proxy_activity,
                    ExternalMCPCallInput(
                        server_url=entry.url,
                        tool_name=operation,
                        arguments=arguments,
                    ),
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(seconds=30),
                )
        except Exception as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                isError=True,
            )

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)]
        )


# -- JSON-RPC helpers ----------------------------------------------------------


def _json_rpc_result(
    request: types.JSONRPCRequest, result: types.Result
) -> types.JSONRPCResponse:
    return types.JSONRPCResponse.model_validate(
        {"jsonrpc": "2.0", "id": request.id, "result": result.model_dump()}
    )


def _json_rpc_error(
    request: types.JSONRPCRequest, error: types.ErrorData
) -> types.JSONRPCResponse:
    return types.JSONRPCResponse.model_validate(
        {"jsonrpc": "2.0", "id": request.id, "error": error.model_dump()}
    )
