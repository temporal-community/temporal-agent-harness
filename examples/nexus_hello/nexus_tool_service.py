"""Demo Nexus-native MCP tool service — the "all the way through Nexus" counterpart to
tool_server.py.

tool_server.py is a 3rd-party MCP server reached over HTTP, registered with (and called
through) the durable tools gateway. This module is instead a Nexus-native MCP server,
registered directly against a *running agent workflow*'s ``NexusMcpServerRegistry`` (see
justfile's ``register-nexus-tool``) — never touching the gateway at all. When the agent
calls ``demo-nexus_get_lucky_number``, ``WorkflowTransport`` routes it straight through
``workflow.create_nexus_client(...).execute_operation(...)`` — no gateway, no activity, just
Nexus.

Uses ``nexus_mcp_tool`` (a plain typed method -> a fully-wired Nexus operation, no separate
Pydantic model or ``Operation[...]`` declaration needed) combined with
``authoring.MCPOverNexusServiceHandler`` (gets ``list_tools`` for free, derived from
``get_lucky_number`` below).

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
# service/operation delimiter in tool names) — see authoring's _SERVICE_NAME_RE. It also
# doubles as the key NexusMcpServerRegistry stores this endpoint under. Given explicitly here
# (rather than left to nexus_mcp_tool's synthesized-from-class-name default) since it becomes
# the tool-name prefix shown to the LLM — see nexus_mcp_tool's docstring.
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
