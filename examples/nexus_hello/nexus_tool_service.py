"""Demo native Nexus service that exposes MCP-shaped tools.

Demonstrates authoring ergonomics with Nexus MCP:
- `@nexus_mcp_tool` decorator marks methods as tools.
- `@nexus_mcp_operation` exposes a workflow-backed Nexus operation as a tool.
- `MCPOverNexusServiceHandler` base class derives list_tools from the decorated methods.

Run the worker with (from the repo root):
    uv run --extra nexus-mcp python -m examples.nexus_hello.nexus_tool_service
"""

from __future__ import annotations

import asyncio
import random

import nexusrpc.handler
import temporalio.nexus
from mcp.types import ToolAnnotations
from nexus_mcp.authoring import (
    MCPOverNexusServiceHandler,
    nexus_mcp_operation,
    nexus_mcp_tool,
)
from pydantic import BaseModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

# The Nexus endpoint name this service is reached through — created (if missing) by
# `just setup-nexus`, which maps it to NEXUS_TASK_QUEUE on this namespace.
NEXUS_ENDPOINT = "nexus-hello-demo-endpoint"
NEXUS_TASK_QUEUE = "nexus-hello-nexus-tools"

# The service name and the full generated tool name must each match
# [a-zA-Z0-9_-]{1,64}. A hyphen is valid in the service name.
SERVICE_NAME = "demo-nexus"


class DelayedLuckyNumberInput(BaseModel):
    """Set the topic and delay for a durable lucky-number request."""

    topic: str
    delay_seconds: float = 5.0


class DelayedLuckyNumberOutput(BaseModel):
    """Contain the result of a durable lucky-number request."""

    message: str


@workflow.defn(sandboxed=False)
class DelayedLuckyNumberWorkflow:
    """Wait with a durable timer before it returns a lucky number."""

    @workflow.run
    async def run(self, input: DelayedLuckyNumberInput) -> DelayedLuckyNumberOutput:
        await workflow.sleep(input.delay_seconds)
        number = workflow.random().randint(1, 100)
        return DelayedLuckyNumberOutput(
            message=f"{input.topic}'s delayed lucky number is {number}."
        )


@nexusrpc.handler.service_handler(name=SERVICE_NAME)
class DemoNexusToolsServiceHandler(MCPOverNexusServiceHandler):
    """Expose short and workflow-backed tools directly through Nexus.

    MCPOverNexusServiceHandler derives list_tools from the marked operations.
    """

    # This decorator creates the input model and the Nexus operation.
    # Allows author to write a simple method and have it exposed as a Nexus tool
    # without caring too much about SerDe or Nexus operation details.
    @nexus_mcp_tool
    async def get_lucky_number(self, topic: str) -> str:
        """Return a (canned) lucky number for the given topic."""
        return f"{topic}'s lucky number today is {random.randint(1, 100)}."

    # This decorator exposes an existing Nexus operation as an MCP tool.
    # Allows more advanced user to define their input/output model and also
    # have access to the operation context, etc...
    @nexus_mcp_operation(
        title="Get a delayed lucky number",
        annotations=ToolAnnotations(
            read_only_hint=True,
            idempotent_hint=True,
        ),
    )
    @temporalio.nexus.workflow_run_operation
    async def get_delayed_lucky_number(
        self,
        ctx: temporalio.nexus.WorkflowRunOperationContext,
        input: DelayedLuckyNumberInput,
    ) -> temporalio.nexus.WorkflowHandle[DelayedLuckyNumberOutput]:
        """Return a lucky number after a durable delay."""
        return await ctx.start_workflow(
            DelayedLuckyNumberWorkflow.run,
            input,
            id=f"delayed-lucky-number-{ctx.request_id}",
            task_queue=NEXUS_TASK_QUEUE,
        )


async def main() -> None:
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config)

    worker = Worker(
        client,
        task_queue=NEXUS_TASK_QUEUE,
        workflows=[DelayedLuckyNumberWorkflow],
        nexus_service_handlers=[DemoNexusToolsServiceHandler()],
    )
    print(
        f"Demo Nexus tool service ready: endpoint={NEXUS_ENDPOINT!r} "
        f"taskQueue={NEXUS_TASK_QUEUE!r}.",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
