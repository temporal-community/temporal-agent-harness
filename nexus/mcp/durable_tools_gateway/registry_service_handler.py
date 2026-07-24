"""RegistryServiceHandler - implementation of RegistryService operations.

The Durable Tools Gateway's Nexus-facing surface: lets callers in any
namespace register a 3rd-party (non-Nexus) MCP server, and lets a calling
agent workflow invoke that server's tools through here — durably, via
``ToolCallWorkflow`` (the same per-call durability wrapper ``InboundGateway``
uses for its own raw-HTTP callers), NOT a standalone activity: standalone
activities (``Client.execute_activity``, no workflow context) need an
experimental server capability (``nexusoperation.enableStandalone``) that's
been observed to deadlock the CALLING workflow in real usage — plain
workflow-wrapped activities are the well-tested path.

Nexus-native MCP servers never touch this service at all: they register and
are called directly by the calling agent workflow (see
``temporal_agent_harness.ai_sdks.openai_agents``'s ``NexusMcpServerRegistry``).
"""

from __future__ import annotations

import uuid

import nexusrpc
import nexusrpc.handler
import temporalio.nexus
from nexusrpc.handler import StartOperationContext
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy

from .activities import ExternalMCPCallInput
from .registry import REGISTRY_WORKFLOW_ID, RegistryEntry, ToolRegistryWorkflow
from .registry_service import (
    CallToolInput,
    CallToolOutput,
    DeregisterInput,
    ListToolsOutput,
    RegisterExternalInput,
    RegistryService,
)
from .tool_call import ToolCallWorkflow

# Nexus endpoint name - cross-namespace callable.
REGISTRY_NEXUS_ENDPOINT = "mcp-registry-endpoint"


@nexusrpc.handler.service_handler(service=RegistryService)
class RegistryServiceHandler:
    """Signals/queries ToolRegistryWorkflow, and dispatches tool calls, on
    behalf of callers from any namespace.

    Service workers in other namespaces reach this handler via the
    ``mcp-registry-endpoint`` Nexus endpoint.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @nexusrpc.handler.sync_operation
    async def register_external(
        self, ctx: StartOperationContext, input: RegisterExternalInput
    ) -> None:
        """Register a 3rd-party external MCP server with the gateway."""
        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        await handle.signal(
            ToolRegistryWorkflow.register_external,
            args=[input.name, input.url],
        )

    @nexusrpc.handler.sync_operation
    async def deregister(
        self, ctx: StartOperationContext, input: DeregisterInput
    ) -> None:
        """Remove a service registration from the gateway."""
        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        await handle.signal(ToolRegistryWorkflow.deregister, args=[input.name])

    @nexusrpc.handler.sync_operation
    async def list_tools(
        self, ctx: StartOperationContext, input: None
    ) -> ListToolsOutput:
        """Return all tool dicts for 3rd-party servers registered with the gateway.

        Deliberately does NOT extend ``authoring.MCPOverNexusServiceHandler``
        — this tool list comes from servers registered via ``register_external``,
        not from this handler's own Nexus operations.
        """
        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        tools = await handle.query(ToolRegistryWorkflow.list_tools)
        return ListToolsOutput(tools=tools)

    @nexusrpc.handler.sync_operation
    async def call_tool(
        self, ctx: StartOperationContext, input: CallToolInput
    ) -> CallToolOutput:
        """Invoke one tool on a registered 3rd-party MCP server.

        Starts ``ToolCallWorkflow`` (this same package's per-call durability wrapper —
        one workflow, one activity) and awaits its result — NOT a standalone activity
        (``Client.execute_activity``, no workflow context). An earlier version dispatched
        the activity standalone on the theory that the caller (an agent workflow, reaching
        this via Nexus) is already durable, so a sub-workflow would just be a redundant
        extra hop — but standalone activities need an experimental server capability
        (``nexusoperation.enableStandalone``) that's been observed to deadlock the CALLING
        workflow in real usage (confirmed live: "Potential deadlock detected: workflow
        didn't yield within 2 second(s)"). A plain workflow-wrapped activity is the
        well-tested path (the same one ``InboundGateway`` already uses for its own raw-HTTP
        callers), at the cost of one extra, cheap Temporal hop.

        Every failure here is raised as a ``nexusrpc.HandlerError`` with an explicit,
        non-retryable type — NOT a bare exception. An unhandled Python exception escaping a
        Nexus handler defaults to ``HandlerErrorType.UNKNOWN``, which nexusrpc treats as
        *retryable*: the calling workflow's ``execute_operation`` await would then sit retrying
        this operation indefinitely (observed live: "Failed to execute Nexus start operation
        method" repeating every few seconds, no error ever reaching the caller) instead of
        WorkflowTransport's own ``except Exception`` cleanly turning this into an ``isError``
        result the one time it's actually invoked.
        """
        name = input.name or ""
        service, _, operation = name.partition("_")
        if not service or not operation:
            raise nexusrpc.HandlerError(
                f"Invalid tool name {name!r}: expected 'service_operation'",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )

        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        entry: RegistryEntry | None = await handle.query(ToolRegistryWorkflow.find, service)
        if entry is None:
            raise nexusrpc.HandlerError(
                f"Service {service!r} is not registered with the gateway.",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            )

        try:
            call_handle = await self._client.start_workflow(
                ToolCallWorkflow.run,
                ExternalMCPCallInput(
                    server_url=entry.url,
                    tool_name=operation,
                    arguments=input.arguments or {},
                ),
                id=f"mcp-proxy-{uuid.uuid4()}",
                # Dispatch to whichever task queue THIS handler is actually running on, not a
                # hardcoded constant — lets the gateway run on any task queue (e.g. a
                # per-test one), same as every other worker-hosted component here.
                task_queue=temporalio.nexus.info().task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            result = await call_handle.result()
        except WorkflowFailureError as exc:
            raise nexusrpc.HandlerError(
                str(exc.cause or exc),
                type=nexusrpc.HandlerErrorType.INTERNAL,
                retryable_override=False,
            ) from exc
        return CallToolOutput(result=result)
