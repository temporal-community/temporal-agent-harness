"""durable_tools_gateway — the gateway's Nexus-facing surface (RegistryService/Handler,
registration + ToolCallWorkflow-based dispatch) AND its raw-HTTP-facing surface
(InboundGateway, ToolCallWorkflow, for MCP clients with no Temporal durability of their own)
— one package, since both sides of one running gateway process (see ``server.py``) always
ship together.
"""

import nexusrpc
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from .activities import ExternalMCPCallInput, fetch_external_tools, mcp_proxy_activity
    from .inbound import InboundGateway
    from .registry import REGISTRY_TASK_QUEUE, REGISTRY_WORKFLOW_ID, RegistryEntry, ToolRegistryWorkflow
    from .registry_service import (
        CallToolInput,
        CallToolOutput,
        DeregisterInput,
        ListToolsOutput,
        RegisterExternalInput,
        RegistryService,
    )
    from .registry_service_handler import REGISTRY_NEXUS_ENDPOINT, RegistryServiceHandler
    from .tool_call import ToolCallWorkflow

# The gateway's own actual Nexus service name -- register it against a workflow's
# NexusMcpServerRegistry under THIS name (not an arbitrary label), the same convention any
# other Nexus-reachable service already follows. WorkflowTransport discovers direct vs proxy
# dispatch structurally (does a returned tool's own prefix match the name it was registered
# under?), so there's nothing else to declare at registration time.
_registry_service_definition = nexusrpc.get_service_definition(RegistryService)
assert _registry_service_definition is not None, "RegistryService must be @service-decorated"
REGISTRY_SERVICE_NAME = _registry_service_definition.name

__all__ = [
    "REGISTRY_TASK_QUEUE",
    "REGISTRY_WORKFLOW_ID",
    "REGISTRY_SERVICE_NAME",
    "RegistryEntry",
    "ToolRegistryWorkflow",
    "CallToolInput",
    "CallToolOutput",
    "DeregisterInput",
    "ListToolsOutput",
    "RegisterExternalInput",
    "RegistryService",
    "REGISTRY_NEXUS_ENDPOINT",
    "RegistryServiceHandler",
    "ExternalMCPCallInput",
    "fetch_external_tools",
    "mcp_proxy_activity",
    "InboundGateway",
    "ToolCallWorkflow",
]
