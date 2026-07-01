"""Registry activities — run inside the gateway worker."""

from __future__ import annotations

from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from temporalio import activity


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
