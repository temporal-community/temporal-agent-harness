"""
Demo Nexus-native MCP server exposing one tool, reached directly through Nexus from an
MCP client.

Demonstrates authoring ergonomics with Nexus MCP, with:
- `@nexus_mcp_tool` decorator to mark methods as tools, and
- `MCPOverNexusServiceHandler` base class to provide a default list_tools implementation
  that derives all tools from the decorated methods.

Run the worker with (from the repo root):
    uv run --extra nexus-mcp python -m examples.nexus_hello.nexus_tool_service
"""

from __future__ import annotations

import asyncio
import random

import nexusrpc.handler
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from authoring import MCPOverNexusServiceHandler, nexus_mcp_tool

# The Nexus endpoint name this service is reached through — created (if missing) by
# `just setup-nexus`, which maps it to NEXUS_TASK_QUEUE on this namespace.
NEXUS_ENDPOINT = "nexus-hello-demo-endpoint"
NEXUS_TASK_QUEUE = "nexus-hello-nexus-tools"

# Service name must match [a-zA-Z0-9-]{1,64} (no underscores -- underscore is the
# service/operation delimiter in tool names).
# TODO: Figure out a better regex matching strategy.
SERVICE_NAME = "demo-nexus"


@nexusrpc.handler.service_handler(name=SERVICE_NAME)
class DemoNexusToolsServiceHandler(MCPOverNexusServiceHandler):
    """A demo Nexus-native MCP server exposing one tool, reached directly through Nexus.
    list_tools comes for free from MCPOverNexusServiceHandler, derived from get_lucky_number."""

    @nexus_mcp_tool
    async def get_lucky_number(self, topic: str) -> str:
        """Return a (canned) lucky number for the given topic."""
        return f"{topic}'s lucky number today is {random.randint(1, 100)}."


async def main() -> None:
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config)

    worker = Worker(
        client,
        task_queue=NEXUS_TASK_QUEUE,
        nexus_service_handlers=[DemoNexusToolsServiceHandler()],
    )
    print(
        f"Demo Nexus tool service ready: endpoint={NEXUS_ENDPOINT!r} "
        f"taskQueue={NEXUS_TASK_QUEUE!r}. Register it against a running agent workflow with "
        f"`just register-nexus-tool <agent-workflow-id>`.",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
