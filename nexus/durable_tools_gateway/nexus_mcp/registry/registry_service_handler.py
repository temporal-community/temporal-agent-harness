"""RegistryServiceHandler - implementation of RegistryService operations.

Allows users to register their MCP servers to the gateway, whether it be:
- a Nexus-backed MCP server, which the gateway will use Nexus to reach, or
- an externally hosted MCP server, which the gateway will act as a proxy
  between MCP client -> MCP server, providing durability and idempotency
  to calling these tools.
"""

from __future__ import annotations

import nexusrpc.handler
from nexusrpc.handler import StartOperationContext
from temporalio.client import Client

from .registry_service import (
    DeregisterInput,
    RegisterExternalInput,
    RegisterNexusInput,
    RegistryService,
)
from .registry import REGISTRY_WORKFLOW_ID, ToolRegistryWorkflow

# Nexus endpoint name - cross-namespace callable.
REGISTRY_NEXUS_ENDPOINT = "mcp-registry-endpoint"


@nexusrpc.handler.service_handler(service=RegistryService)
class RegistryServiceHandler:
    """Signals ToolRegistryWorkflow on behalf of callers from any namespace.

    Service workers in other namespaces reach this handler via the
    ``mcp-registry-endpoint`` Nexus endpoint.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @nexusrpc.handler.sync_operation
    async def register_nexus(
        self, ctx: StartOperationContext, input: RegisterNexusInput
    ) -> None:
        """Register a 1st-party Nexus service with the gateway."""
        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        await handle.signal(
            ToolRegistryWorkflow.register_nexus,
            args=[input.name, input.endpoint, input.tools],
        )

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
