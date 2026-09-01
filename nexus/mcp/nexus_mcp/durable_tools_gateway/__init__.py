import nexusrpc
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from .registry import (
        REGISTRY_TASK_QUEUE,
        REGISTRY_WORKFLOW_ID_PREFIX,
        AccountEntries,
        AgentRegistration,
        SessionRecord,
        ToolRegistryWorkflow,
        account_registry_workflow_id,
        fetch_external_tools,
    )
    from .generated import (
        CallToolInput,
        CallToolOutput,
        DeregisterInput,
        ListAccountEntriesInput,
        ListAccountEntriesOutput,
        RegisterExternalInput,
        RegistryService,
    )
    from .registry_service_handler import (
        REGISTRY_NEXUS_ENDPOINT,
        ExternalMCPCallInput,
        GatewayA2AServiceHandler,
        RegistryServiceHandler,
        mcp_proxy_activity,
    )

# The gateway's real Nexus service name -- callers reach it via this name + a Nexus
# endpoint, not by importing this module directly.
_registry_service_definition = nexusrpc.get_service_definition(RegistryService)
assert _registry_service_definition is not None, "RegistryService must be @service-decorated"
REGISTRY_SERVICE_NAME = _registry_service_definition.name

__all__ = [
    "REGISTRY_NEXUS_ENDPOINT",
    "REGISTRY_SERVICE_NAME",
    "REGISTRY_TASK_QUEUE",
    "REGISTRY_WORKFLOW_ID_PREFIX",
    "AccountEntries",
    "AgentRegistration",
    "CallToolInput",
    "CallToolOutput",
    "DeregisterInput",
    "ExternalMCPCallInput",
    "GatewayA2AServiceHandler",
    "ListAccountEntriesInput",
    "ListAccountEntriesOutput",
    "RegisterExternalInput",
    "RegistryService",
    "RegistryServiceHandler",
    "SessionRecord",
    "ToolRegistryWorkflow",
    "account_registry_workflow_id",
    "fetch_external_tools",
    "mcp_proxy_activity",
]
