"""WorkflowTransport — client-side MCP integration path.

Instead of requiring an HTTP server + public URL (ngrok) for the MCP gateway,
WorkflowTransport lets the agent talk to the gateway in-process:

  - ``gateway_list_tools_activity`` fetches tools from ToolRegistryWorkflow.
  - ``gateway_call_tool_activity`` dispatches a call through ToolCallWorkflow.

The agent fetches tools once per turn and passes them to Gemini as function
declarations.  When Gemini returns FunctionCallStep events the agent executes
them by dispatching through these activities, which carry the same durability
guarantees as the HTTP path (content-hash stable workflow IDs, USE_EXISTING).

Initialise at worker startup::

    from mcp_gateway.transport import init_transport
    init_transport(temporal_client)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError

from registry import REGISTRY_TASK_QUEUE, REGISTRY_WORKFLOW_ID, RegistryEntry, ToolRegistryWorkflow
from .tool_call import MCPServerKind, ToolCallWorkflow, ToolCallWorkflowInput


def _stable_id(name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps({"tool": name, "args": arguments}, sort_keys=True, separators=(",", ":"))
    return f"mcp-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"

_client: Client | None = None


def init_transport(client: Client) -> None:
    """Wire the Temporal client used by the transport activities."""
    global _client
    _client = client


def _get_client() -> Client:
    if _client is None:
        raise RuntimeError("init_transport() must be called before using transport activities")
    return _client


@activity.defn
async def registry_find_activity(service: str) -> RegistryEntry | None:
    """Look up a service entry in ToolRegistryWorkflow by name."""
    client = _get_client()
    handle = client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
    return await handle.query(ToolRegistryWorkflow.find, service)


@activity.defn
async def gateway_list_tools_activity() -> list[dict[str, Any]]:
    """Fetch the current tool list from ToolRegistryWorkflow."""
    client = _get_client()
    handle = client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
    return await handle.query(ToolRegistryWorkflow.list_all_tools)


@activity.defn
async def gateway_call_tool_activity(name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call through ToolCallWorkflow (same path as the HTTP gateway)."""
    client = _get_client()

    service, _, operation = name.partition("_")
    if not service or not operation:
        raise ApplicationError(
            f"Invalid tool name {name!r}: expected 'service_operation' format",
            non_retryable=True,
        )

    registry_handle = client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
    entry = await registry_handle.query(ToolRegistryWorkflow.find, service)
    if entry is None:
        raise ApplicationError(
            f"Service {service!r} is not registered in the gateway",
            non_retryable=True,
        )

    wf_id = _stable_id(name, arguments)

    if entry.kind == "nexus":
        wf_input = ToolCallWorkflowInput(
            kind=MCPServerKind.NEXUS,
            service=service,
            operation=operation,
            endpoint=entry.endpoint,
            arguments=arguments,
        )
    else:
        wf_input = ToolCallWorkflowInput(
            kind=MCPServerKind.EXTERNAL,
            server_url=entry.url,
            tool_name=operation,
            arguments=arguments,
        )

    handle = await client.start_workflow(
        ToolCallWorkflow.run,
        wf_input,
        id=wf_id,
        task_queue=REGISTRY_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    return await handle.result()
