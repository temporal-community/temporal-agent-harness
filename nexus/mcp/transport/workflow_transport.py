"""WorkflowTransport: In-process MCP transport backed by Temporal Nexus.

Every tool source is a native Nexus service, reachable directly. MCP client <-> server
calls go straight through a Nexus call instead of a real MCP session.

Usage inside a Temporal workflow::

    transport = WorkflowTransport({"my-service": "my-endpoint"})
    tools  = await transport.list_tools()
    result = await transport.call_tool("tool_name", {"url": "..."})
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pydantic
from authoring import LIST_TOOLS_OPERATION
from mcp import types
from temporalio import workflow


class WorkflowTransport:
    """In-process MCP transport backed uniformly by Temporal Nexus.

    Args:
        registered_servers: ``{name: endpoint}`` map of native Nexus services, each
                            reachable directly under its own name.
        name: A readable name for this transport instance.
        allowed_servers: If given, restricts this transport to only these registered
                         names. ``None`` (the default) sees everything registered.
    """

    def __init__(
        self,
        registered_servers: Mapping[str, str],
        name: str = "workflow-transport",
        allowed_servers: frozenset[str] | None = None,
    ) -> None:
        self._registered_servers = registered_servers
        self._name = name
        self._allowed_servers = allowed_servers
        # Full tool name -> (registered name, endpoint), rebuilt on every list_tools().
        self._tool_routes: dict[str, tuple[str, str]] = {}

    @property
    def name(self) -> str:
        return self._name

    # -- Public API ----------------------------------------------------------------

    async def list_tools(self) -> list[types.Tool]:
        """Fan out across every registered entry, and rebuild the tool route table
        from whatever answered for each tool."""

        async def _fetch(name: str, endpoint: str) -> list[dict[str, Any]]:
            client = workflow.create_nexus_client(service=name, endpoint=endpoint)
            result: Any = await client.execute_operation(LIST_TOOLS_OPERATION, None)
            return result.get("tools", []) if isinstance(result, dict) else []

        # Snapshot so a concurrent registration doesn't change the set mid-fan-out.
        servers = dict(self._registered_servers)
        if self._allowed_servers is not None:
            servers = {
                name: endpoint
                for name, endpoint in servers.items()
                if name in self._allowed_servers
            }
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
                new_routes[tool_dict["name"]] = (name, endpoint)
                tool_dicts.append(tool_dict)
        self._tool_routes = new_routes

        return [types.Tool(**d) for d in tool_dicts]

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> types.CallToolResult:
        """Direct Nexus call to whichever entry answered for this tool in the last
        list_tools() call."""
        params_kwargs: dict[str, Any] = {"name": tool_name, "arguments": arguments}
        if meta is not None:
            params_kwargs["_meta"] = types.RequestParams.Meta.model_validate(meta)
        return await self._call_tool(types.CallToolRequestParams(**params_kwargs))

    # -- Implementation details --------------------------------------------------

    async def _call_tool(self, params: types.CallToolRequestParams) -> types.CallToolResult:
        route = self._tool_routes.get(params.name)
        if route is None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Tool {params.name!r} is not registered.")],
                isError=True,
            )
        registered_name, endpoint = route
        operation = params.name.removeprefix(f"{registered_name}_")
        arguments = params.arguments or {}
        try:
            client = workflow.create_nexus_client(service=registered_name, endpoint=endpoint)
            result: Any = await client.execute_operation(operation, arguments)
            return _coerce_call_tool_result(result)
        except Exception as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                isError=True,
            )


def _coerce_call_tool_result(value: Any) -> types.CallToolResult:
    """Turn a Nexus operation's raw return value into a real ``types.CallToolResult``.

    A dict already shaped like one (has a ``content`` key -- from
    ``CallToolResult.model_dump()``) round-trips with full fidelity. Anything else (a
    plain string, a pydantic model, ``None``, ...) is wrapped as a single text content
    block.
    """
    if isinstance(value, dict) and "content" in value:
        try:
            return types.CallToolResult.model_validate(value)
        except pydantic.ValidationError:
            pass  # Not actually a well-formed CallToolResult -- fall through and stringify.

    text = (
        value if isinstance(value, str)
        else value.model_dump_json(indent=2) if hasattr(value, "model_dump_json")
        else "" if value is None
        else str(value)
    )
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])
