"""Demo 3rd-party MCP server for the Nexus-hello example.

Stands in for a real external MCP server. Registered with the Durable Tools Gateway's
ToolRegistryWorkflow (seeded on gateway startup — see durable_tools_gateway/worker.py)
and reached at call time through mcp_proxy_activity.

Run with (from the repo root):
    uv run --extra nexus-mcp python -m examples.nexus_hello.tool_server
"""

from __future__ import annotations

import random

from mcp.server.fastmcp import FastMCP

PORT = 8765

mcp = FastMCP(
    "demo-tools",
    host="127.0.0.1",
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
)


@mcp.tool()
def get_fun_fact(topic: str) -> str:
    """Return a (canned) fun fact about the given topic."""
    facts = [
        f"{topic} was mentioned in a movie script exactly once, allegedly.",
        f"The word '{topic}' has more syllables when you say it slowly.",
        f"Scientists remain divided on whether {topic} is interesting.",
        f"{topic} shares a birthday with at least one famous raccoon.",
    ]
    return random.choice(facts)


if __name__ == "__main__":
    print(f"Demo MCP tool server ready: http://127.0.0.1:{PORT}/mcp", flush=True)
    mcp.run(transport="streamable-http")
