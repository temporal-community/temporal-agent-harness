import nexusrpc
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from .registry import (
        REGISTRY_TASK_QUEUE,
        REGISTRY_WORKFLOW_ID,
        RegisterExternalWorkflow,
        RegisterExternalWorkflowInput,
        RegistryEntry,
        ToolRegistryWorkflow,
        fetch_external_tools,
    )
    from .generated import (
        CallToolInput,
        CallToolOutput,
        DeregisterInput,
        ListToolsOutput,
        RegisterExternalInput,
        RegistryService,
    )
    from .registry_service_handler import (
        REGISTRY_NEXUS_ENDPOINT,
        ExternalMCPCallInput,
        RegistryServiceHandler,
        ToolCallWorkflow,
        mcp_proxy_activity,
    )

# The gateway's own actual Nexus service name -- register it against a workflow's
# NexusMcpServerRegistry under THIS name (not an arbitrary label), the same convention any
# other Nexus-reachable service already follows. WorkflowTransport discovers direct vs proxy
# dispatch structurally (does a returned tool's own prefix match the name it was registered
# under?), so there's nothing else to declare at registration time.
_registry_service_definition = nexusrpc.get_service_definition(RegistryService)
assert _registry_service_definition is not None, "RegistryService must be @service-decorated"
REGISTRY_SERVICE_NAME = _registry_service_definition.name

__all__ = [
    "REGISTRY_NEXUS_ENDPOINT",
    "REGISTRY_SERVICE_NAME",
    "REGISTRY_TASK_QUEUE",
    "REGISTRY_WORKFLOW_ID",
    "CallToolInput",
    "CallToolOutput",
    "DeregisterInput",
    "ExternalMCPCallInput",
    "ListToolsOutput",
    "RegisterExternalInput",
    "RegisterExternalWorkflow",
    "RegisterExternalWorkflowInput",
    "RegistryEntry",
    "RegistryService",
    "RegistryServiceHandler",
    "ToolCallWorkflow",
    "ToolRegistryWorkflow",
    "fetch_external_tools",
    "mcp_proxy_activity",
]
