"""Registry activities — run inside the gateway worker.

``mcp_proxy_activity`` is always dispatched via ``ToolCallWorkflow`` (this package's own
per-call durability wrapper) — both by ``RegistryServiceHandler.call_tool`` (the gateway's
own Nexus operation, called by an already-durable caller like ``WorkflowTransport``) and by
``InboundGateway``'s raw-HTTP callers, which have no durability of their own. See
``registry_service_handler.py``'s ``call_tool`` docstring for why an earlier version
dispatched it as a standalone activity instead.
"""

from __future__ import annotations

from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel
from temporalio import activity


class ExternalMCPCallInput(BaseModel):
    """Input for an HTTP call to a 3rd-party MCP server."""

    server_url: str
    tool_name: str
    arguments: dict[str, Any]


@activity.defn
async def fetch_external_tools(name: str, url: str) -> list[dict[str, Any]]:
    """Fetch and prefix the tool list from an external Streamable-HTTP MCP server.

    Args:
        name: Service name prefix, e.g. "weather".  Each tool returned by
              the server is renamed {name}_{original_tool_name}.
        url:  Streamable-HTTP MCP endpoint, e.g.
              "https://weather-mcp.example.com/mcp".

    Returns:
        List of serialised mcp.types.Tool dicts with prefixed names.
    """
    activity.logger.info("[registry] fetching tools from %s", url)
    activity.heartbeat()

    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()

    tools = []
    for tool in result.tools:
        prefixed = tool.model_copy(update={"name": f"{name}_{tool.name}"})
        tools.append(prefixed.model_dump())

    activity.logger.info("[registry] fetched %d tool(s) from %s", len(tools), url)
    return tools


@activity.defn
async def mcp_proxy_activity(input: ExternalMCPCallInput) -> str:
    """Call a tool on an external MCP server over Streamable HTTP.

    Standalone — startable directly from ``RegistryServiceHandler.call_tool``
    (a Nexus operation handler, not a workflow) as well as from a workflow
    (``ToolCallWorkflow``, this package's own per-call durability wrapper).
    """
    activity.logger.info(
        "[proxy-activity] calling %r on %s", input.tool_name, input.server_url
    )
    activity.heartbeat()

    async with streamable_http_client(input.server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(input.tool_name, input.arguments)

    activity.logger.info(
        "[proxy-activity] %r completed  is_error=%s", input.tool_name, result.isError
    )
    return _call_tool_result_to_str(result)


def _call_tool_result_to_str(result: Any) -> str:
    if result.isError:
        texts = [
            c.text for c in (result.content or []) if hasattr(c, "text") and c.text
        ]
        raise RuntimeError(texts[0] if texts else "MCP tool returned an error")
    parts: list[str] = []
    for c in result.content or []:
        if hasattr(c, "text") and c.text:
            parts.append(c.text)
        elif hasattr(c, "data"):
            parts.append(f"[binary data: {len(c.data)} bytes]")
        else:
            parts.append(str(c))
    return "\n".join(parts)
