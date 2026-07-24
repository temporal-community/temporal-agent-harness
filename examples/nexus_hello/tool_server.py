"""Tiny demo 3rd-party MCP server for the Nexus-transport example.

Stands in for whatever real external MCP server you'd register with the durable tool call
gateway. This process is never talked to directly by the agent workflow — it's registered
with the gateway's ToolRegistryWorkflow (see register_tool.py) and reached at call time
through mcp_proxy_activity, an activity dispatched by nexus_transport_mcp_server's
WorkflowTransport.

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


# structured_output=False: FastMCP would otherwise auto-generate an outputSchema from the
# `-> str` annotation. nexus_mcp's external-tool round trip (mcp_proxy_activity + how
# WorkflowTransport rebuilds the CallToolResult on the way back) currently drops
# structuredContent, so a tool WITH an outputSchema fails the MCP client's own
# "declared an outputSchema but result carries no structuredContent" validation. Not a bug
# in this example or in nexus_transport_mcp_server — it's a gap in nexus_mcp itself; this
# just avoids tripping it for the demo tool.
@mcp.tool(structured_output=False)
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
