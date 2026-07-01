"""InboundGateway - bridges MCP server requests to Temporal Nexus operations.

Every tool call request is executed as a ToolCallWorkflow with a
content-hash stable ID and id_conflict_policy=USE_EXISTING.  This gives
three crash-safety properties:

  1. MCP server crashes   -> workflow retries the Nexus call / activity
  2. Gateway crashes      -> completed workflow result is in Temporal history;
                            retry finds it via the same stable ID
  3. HTTP disconnect      -> workflow keeps running; retry reconnects to it

See tool_call.py for the workflow implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy

from .tool_call import MCPServerKind, ToolCallWorkflow, ToolCallWorkflowInput
from registry import REGISTRY_TASK_QUEUE, REGISTRY_WORKFLOW_ID, RegistryEntry, ToolRegistryWorkflow

logger = logging.getLogger(__name__)

# Virtual tool exposed so MCP clients that don't re-call tools/list mid-session
# (Claude, Codex, ...) can dynamically discover newly registered tools at runtime.
# TODO: See if this is the best way to do this, I just didn't wanna repeat the _handle_list_tools(...)
#       implementation.
_LIST_AVAILABLE_TOOLS = types.Tool(
    name="list_available_tools",
    description=(
        "Return all tools currently registered with this gateway. "
        "Call this when you need to discover tools that may have been "
        "added since the session started."
    ),
    inputSchema={"type": "object", "properties": {}},
)


class InboundGateway:
    """Bridge MCP tool requests to Temporal via ToolCallWorkflow."""

    def __init__(self, client: Client, endpoint: str) -> None:
        """
        Args:
            client: Connected Temporal client.
            endpoint: Name of the Nexus endpoint that hosts the MCP services
                      (e.g. "qa-tools-endpoint").  Used for tool routing; not
                      used for list_tools (served by the gateway itself).
        """
        self._client = client
        self._endpoint = endpoint
        self._known_tools: frozenset[str] = frozenset()

    # -- Public API -----------------------------------------------------------

    def register(self, mcp_server: Server) -> None:
        """Wire the gateway's handlers into a low-level MCP Server."""
        mcp_server.list_tools()(self._handle_list_tools)  # type: ignore[no-untyped-call]
        mcp_server.call_tool()(self._handle_call_tool)

    # -- MCP handlers ---------------------------------------------------------

    async def _handle_list_tools(self) -> list[types.Tool]:
        """Return all registered tools by querying ToolRegistryWorkflow directly."""
        logger.info("[gateway] list_tools called, querying ToolRegistryWorkflow")
        try:
            handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
            tool_dicts: list[dict] = await handle.query(
                ToolRegistryWorkflow.list_all_tools
            )
            tools = [types.Tool(**d) for d in tool_dicts]
        except Exception as exc:
            logger.debug("list_tools: registry query failed (%s)", exc)
            tools = []
        self._log_tool_changes(tools)
        return [*tools, _LIST_AVAILABLE_TOOLS]

    def _log_tool_changes(self, tools: list[types.Tool]) -> None:
        """Print a line whenever the registered tool set changes."""
        current = frozenset(t.name for t in tools)
        if current == self._known_tools:
            return
        added = sorted(current - self._known_tools)
        removed = sorted(self._known_tools - current)
        if added:
            print(f"[gateway] tools registered:   {added}", flush=True)
        if removed:
            print(f"[gateway] tools deregistered: {removed}", flush=True)
        self._known_tools = current

    async def _handle_call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> list[types.TextContent] | types.CallToolResult:
        """Execute a tool call as a durable ToolCallWorkflow."""
        if name == _LIST_AVAILABLE_TOOLS.name:
            tools = await self._handle_list_tools()
            summary = json.dumps(
                [{"name": t.name, "description": t.description} for t in tools],
                indent=2,
            )
            return [types.TextContent(type="text", text=summary)]

        service, _, operation = name.partition("_")
        if not service or not operation:
            raise ValueError(
                f"Invalid tool name {name!r}: expected 'service_operation' format"
            )

        entry = await self._registry_find(service)
        if entry is None:
            raise ValueError(
                f"Service {service!r} is not registered in the gateway. "
                f"Start its worker (which self-registers on startup) or run "
                f"'just register-mcp {service} <url>' for an external server."
            )

        wf_id = _stable_workflow_id(name, arguments, entry)

        if entry.kind == "nexus":
            wf_input = ToolCallWorkflowInput(
                kind=MCPServerKind.NEXUS,
                service=service,
                operation=operation,
                endpoint=entry.endpoint,
                arguments=arguments,
            )
            logger.info(
                "[gateway] call_tool nexus  service=%r  op=%r  endpoint=%r  wf=%s",
                service, operation, entry.endpoint, wf_id,
            )
        else:
            wf_input = ToolCallWorkflowInput(
                kind=MCPServerKind.EXTERNAL,
                server_url=entry.url,
                tool_name=operation,
                arguments=arguments,
            )
            logger.info(
                "[gateway] call_tool external  service=%r  op=%r  url=%s  wf=%s",
                service, operation, entry.url, wf_id,
            )

        try:
            handle = await self._client.start_workflow(
                ToolCallWorkflow.run,
                wf_input,
                id=wf_id,
                task_queue=REGISTRY_TASK_QUEUE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            result_str = await handle.result()
            return [types.TextContent(type="text", text=result_str)]
        except Exception as exc:
            return _tool_error(exc)

    async def _registry_find(self, service: str) -> RegistryEntry | None:
        try:
            handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
            return await handle.query(ToolRegistryWorkflow.find, service)
        except Exception as exc:
            logger.debug("registry query failed for %r: %s", service, exc)
            return None


def _stable_workflow_id(tool_name: str, arguments: dict[str, Any], registry_entry: RegistryEntry) -> str:
    """Derive a deterministic workflow ID from the tool call content.

    Used by InboundGateway to set the ToolCallWorkflow ID so that a
    gateway crash + client retry reconnects to the already-running (or
    completed) workflow instead of starting a duplicate.
    """
    canonical = json.dumps(
        {"tool": tool_name, "args": arguments}, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]

    # For ease of debugging, encode data about the outbound call.
    if registry_entry.kind == "nexus":
        return f"mcp-{registry_entry.kind}-{registry_entry.endpoint}-{tool_name}-{digest}"
    
    return f"mcp-{registry_entry.url}-{tool_name}-{digest}"



# -- Error utils --------------------------------------------------------------


def _tool_error(exc: Exception) -> types.CallToolResult:
    root = _root_cause(exc)
    msg = str(root)
    logger.info("[gateway] tool error — %s: %s", type(root).__name__, msg)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=msg)],
        isError=True,
    )


def _root_cause(exc: Exception) -> Exception:
    cause: Exception = exc
    while cause.__cause__ is not None and isinstance(cause.__cause__, Exception):
        cause = cause.__cause__
    return cause
