"""WorkflowTransport — proper MCP transport for Temporal workflows.

Follows the shape of https://github.com/bergundy/nexus-mcp-python. Every tool source is
reached over Nexus, uniformly, and registered against the calling agent workflow itself the
same way (see this package's caller, ``temporal_agent_harness.ai_sdks.openai_agents``'s
``NexusMcpServerRegistry``) — a name (which must be that service's own actual Nexus service
name) and the endpoint that reaches it. There is no separate, statically-configured "gateway"
concept, and nothing else to declare at registration time — a registered service is either:

  - A Nexus-native MCP server (one that implements ``authoring.MCPOverNexusService``) — a
    statically-known, author-time-fixed set of tools, each its own Nexus operation, called
    DIRECTLY: ``workflow.create_nexus_client(service=<its own name>, endpoint=<its
    endpoint>)``. No proxy, no activity, one Temporal hop.

  - A proxy (e.g. the Durable Tools Gateway) fronting a *dynamically*-discovered tool set it
    can't know ahead of time, so it necessarily exposes a generic
    ``list_tools``/``call_tool(name, args)`` pair instead of one static operation per tool.

``_handle_list_tools`` fans the ``list_tools`` call out across every registered entry (both
kinds implement the same ``list_tools`` contract — see ``authoring``) and, for each returned
tool, records which registered entry actually answered for it. That's also how a proxy is
told apart from a direct server — with no role to declare anywhere: a direct server's own
tools are always prefixed with its own registered name (the same requirement that already
lets ``WorkflowTransport`` call it back at all), so any tool prefix that DOESN'T match the
name it came from is necessarily a proxy answering on someone else's behalf, and gets called
back through the generic ``call_tool`` contract instead. From the calling agent's perspective
there's exactly one uniform tool menu regardless of where each tool actually lives.

Usage inside a Temporal workflow::

    transport = WorkflowTransport(registered_servers=registry.servers)
    async with transport.connect() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools  = await session.list_tools()
            result = await session.call_tool("docsproxy_read_page", {"url": "..."})
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Mapping

import anyio
import anyio.streams.memory
import mcp.types as types
import pydantic
from mcp.shared.message import SessionMessage
from temporalio import workflow

from authoring import CALL_TOOL_OPERATION, LIST_TOOLS_OPERATION


class WorkflowTransport:
    """In-process MCP transport backed uniformly by Temporal Nexus.

    No HTTP server or public URL required on the caller side, and no raw
    cross-namespace Temporal client either: every hop that leaves this
    workflow's own namespace goes through a Nexus endpoint.

    Args:
        registered_servers: Live ``{name: endpoint}`` view of every Nexus-reachable MCP tool
                          source registered directly against the calling agent workflow —
                          Nexus-native servers and, if any, proxies (e.g. the Durable Tools
                          Gateway) alike; see the module docstring for how the two are told
                          apart, with nothing to declare here. Read at call time, so
                          registrations/deregistrations that land between ``connect()`` and a
                          given call are picked up immediately — this is expected to be the
                          same dict object a ``NexusMcpServerRegistry`` mutates in place, not
                          a snapshot copy.
        enabled_services: Optional live getter returning the service names
                          currently opted into for this session — being
                          *reachable* (present in ``registered_servers``) does not make a
                          service *available*; both ``list_tools`` and ``call_tool``
                          are filtered against this set. ``None`` (the
                          default) disables filtering entirely — every
                          reachable service is available, matching this
                          class's original behavior. Called fresh on every
                          ``list_tools``/``call_tool``, so a mid-conversation
                          change takes effect immediately.
    """

    def __init__(
        self,
        registered_servers: Mapping[str, str],
        enabled_services: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._registered_servers = registered_servers
        self._enabled_services = enabled_services
        # Rebuilt on every list_tools() call (see _handle_list_tools) -- maps a tool's own
        # prefix to whichever registered (name, endpoint) actually answered for it. Populated
        # lazily/refreshed rather than at __init__ time since it depends on a real Nexus round
        # trip; call_tool relies on list_tools having run at least once first, which the MCP
        # protocol itself already guarantees (a client always lists tools before calling one).
        self._tool_routes: dict[str, tuple[str, str]] = {}

    def _is_enabled(self, service: str) -> bool:
        return self._enabled_services is None or service in self._enabled_services()

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
                # Only swallow OUR OWN cancellation of router_task, above -- if the CURRENT
                # (outer) task is itself being cancelled right now (e.g. Temporal evicting
                # this workflow instance), cancelling router_task above also propagates into
                # it as a side effect, and `await router_task` raises the SAME exception type
                # either way -- no way to tell them apart except by asking the current task
                # directly. Swallowing unconditionally here is exactly the anti-pattern
                # Temporal's own SDK warns about ("Timed out running eviction job ... usually
                # caused by inadvertently catching BaseExceptions like asyncio.CancelledError
                # and still continuing work"): confirmed live, it left eviction unable to ever
                # complete, so a stuck workflow retried the SAME workflow task forever instead
                # of a fresh replay ever getting a chance to make progress.
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    raise
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
        """Fan list_tools out across every registered entry (direct and proxy alike), and
        rebuild the prefix -> (name, endpoint) route table from whatever answered for each
        tool -- see the module docstring for how that tells direct and proxy apart."""

        async def _fetch(name: str, endpoint: str) -> list[dict[str, Any]]:
            client = workflow.create_nexus_client(service=name, endpoint=endpoint)
            result: Any = await client.execute_operation(LIST_TOOLS_OPERATION, None)
            return result.get("tools", []) if isinstance(result, dict) else []

        # Snapshot the registry once so every task below sees the same set of servers, even
        # if a signal mutates it while these calls are in flight. No pre-filter by
        # enabled_services here (unlike before this could optimistically skip a disabled
        # DIRECT server by name) -- a registered name no longer reliably predicts which tool
        # prefixes it'll answer for (a proxy's registered name is its OWN identity, not the
        # prefixes of the tools it fronts), so every registered entry is always queried; the
        # per-tool filter below is what actually enforces the allowlist.
        servers = dict(self._registered_servers)
        names = list(servers.keys())
        results = await asyncio.gather(
            *(_fetch(name, endpoint) for name, endpoint in servers.items()),
            return_exceptions=True,
        )

        tool_dicts: list[dict[str, Any]] = []
        new_routes: dict[str, tuple[str, str]] = {}
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                workflow.logger.warning(
                    "[workflow-transport] list_tools failed for %r: %s", name, result
                )
                continue
            endpoint = servers[name]
            for tool_dict in result:
                service = str(tool_dict.get("name", "")).partition("_")[0]
                new_routes[service] = (name, endpoint)
                if self._is_enabled(service):
                    tool_dicts.append(tool_dict)
        self._tool_routes = new_routes

        return types.ListToolsResult(tools=[types.Tool(**d) for d in tool_dicts])

    async def _handle_call_tool(
        self, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """Route the call using the route table _handle_list_tools last built: direct Nexus
        for a tool prefix that matches its own registered name, the generic call_tool
        contract (-> ToolCallWorkflow, a plain workflow wrapping one activity, for the Durable Tools Gateway) for one that
        doesn't."""
        service, _, operation = params.name.partition("_")
        if not service or not operation:
            return types.CallToolResult(
                content=[types.TextContent(type="text",
                    text=f"Invalid tool name {params.name!r}: expected 'service_operation'")],
                isError=True,
            )
        if not self._is_enabled(service):
            # Deliberately the same shape as "not registered" below -- from the caller's
            # perspective, a disabled service and an unreachable one look identical. Being
            # reachable never implies being available; see enabled_services' docstring.
            return types.CallToolResult(
                content=[types.TextContent(type="text",
                    text=f"Service {service!r} is not enabled for this session.")],
                isError=True,
            )

        arguments = params.arguments or {}
        route = self._tool_routes.get(service)
        if route is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text",
                    text=f"Service {service!r} is not a registered Nexus-native server, "
                         f"and no proxy (e.g. a Durable Tools Gateway) is registered to "
                         f"route it.")],
                isError=True,
            )
        registered_name, endpoint = route
        try:
            client = workflow.create_nexus_client(service=registered_name, endpoint=endpoint)
            if registered_name == service:
                # This entry's own registered name matches the tool's prefix -- a
                # Nexus-native server, called directly via its own operation.
                result: Any = await client.execute_operation(operation, arguments)
                text = (
                    result if isinstance(result, str)
                    else result.model_dump_json(indent=2) if hasattr(result, "model_dump_json")
                    else str(result)
                )
            else:
                # This entry is answering on behalf of a prefix it doesn't own -- a proxy
                # (e.g. the Durable Tools Gateway), called back through the shared generic
                # call_tool contract instead.
                call_result: Any = await client.execute_operation(
                    CALL_TOOL_OPERATION, {"name": params.name, "arguments": arguments}
                )
                text = (
                    call_result.get("result") or ""
                    if isinstance(call_result, dict)
                    else str(call_result)
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
