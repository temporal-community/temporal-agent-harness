"""RegistryServiceHandler -- Nexus-facing surface of the Durable Tools Gateway.

Registers 3rd-party MCP servers under an agent_id and proxies calls to them. This
allows us to manage the bag of tools for different agents under the same gateway.

Responsible for registration of tools as well as implementing the MCP protocol
that proxies calls between the agent and the 3rd party tools, using Temporal.

Specifically, tool listing and calling are done via Nexus + Standalone Activities
like so:

Agent harness --- (Nexus) ---> Gateway --- (Standalone Activity) ---> 3rd-party MCP server

This enables end-to-end durability and visibility of tool calls without requiring the
agent harness to be a workflow, and all credential concerns will be managed by the
gateway.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

import nexusrpc
import nexusrpc.handler
import temporalio.nexus
from mcp.client import Client as MCPClient
from mcp.types import CallToolResult
from nexusrpc.handler import StartOperationContext
from pydantic import BaseModel
from temporalio import activity
from temporalio.client import ActivityFailureError, Client
from temporalio.common import RetryPolicy

from nexus_mcp.authoring import validate_service_name

from .generated import (
    CallToolInput,
    CallToolOutput,
    CallToolOutputResult,
    DeregisterInput,
    ListAgentEntriesInput,
    ListAgentEntriesOutput,
    ListAgentEntriesOutputRemoteTools,
    ListAgentEntriesOutputRemoteToolsValueItem,
    RegisterExternalInput,
    RegistryService,
)
from .registry import (
    REGISTRY_WORKFLOW_ID,
    ToolRegistryWorkflow,
    fetch_external_tools,
)

REGISTRY_NEXUS_ENDPOINT = "mcp-registry-endpoint"

logger = logging.getLogger(__name__)


class ExternalMCPCallInput(BaseModel):
    """Input for an HTTP call to a 3rd-party MCP server."""

    server_url: str
    tool_name: str
    arguments: dict[str, Any]


async def _heartbeat_every(seconds: float) -> None:
    """Heartbeat on a fixed interval until cancelled."""
    while True:
        await asyncio.sleep(seconds)
        activity.heartbeat()


@activity.defn
async def mcp_proxy_activity(input: ExternalMCPCallInput) -> CallToolResult:
    """Call one tool on an external MCP server over Streamable HTTP.

    Returns CallToolResult as-is: a tool-level error (is_error=True) is data, not
    raised. Only a real RPC/transport failure raises.
    """
    activity.logger.info("[proxy-activity] calling %r on %s", input.tool_name, input.server_url)
    heartbeat_task = asyncio.create_task(_heartbeat_every(15))
    try:
        async with MCPClient(input.server_url, mode="auto") as client:
            result = await client.call_tool(input.tool_name, input.arguments)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
    activity.logger.info("[proxy-activity] %r completed  is_error=%s", input.tool_name, result.is_error)
    return result


async def _fetch_tools_grouped(client: Client, servers: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Fetch each {alias: url}'s tool list live, grouped by alias. Unreachable servers
    are logged and skipped."""
    if not servers:
        return {}

    async def _fetch_one(name: str, url: str) -> list[dict[str, Any]]:
        return await client.execute_activity(
            fetch_external_tools,
            args=[name, url],
            id=f"mcp-list-tools-{name}-{uuid.uuid4()}",
            task_queue=temporalio.nexus.info().task_queue,
            start_to_close_timeout=timedelta(seconds=60),
        )

    results = await asyncio.gather(
        *(_fetch_one(name, url) for name, url in servers.items()), return_exceptions=True
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for (name, url), result in zip(servers.items(), results):
        if isinstance(result, BaseException):
            logger.warning("[registry] could not fetch tools from %r (%s): %s", name, url, result)
            continue
        grouped[name] = result
    return grouped


@nexusrpc.handler.service_handler(service=RegistryService)
class RegistryServiceHandler:
    """Signals/queries ToolRegistryWorkflow and dispatches tool calls, for callers in
    any namespace, via the mcp-registry-endpoint Nexus endpoint."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @nexusrpc.handler.sync_operation
    async def register_external(
        self, ctx: StartOperationContext, input: RegisterExternalInput
    ) -> None:
        """Register a 3rd-party MCP server under one agent_id."""
        agent_id = input.agent_id or ""
        name = input.name or ""
        if not agent_id:
            raise nexusrpc.HandlerError("agent_id is required", type=nexusrpc.HandlerErrorType.BAD_REQUEST)
        try:
            validate_service_name(name)
        except ValueError as exc:
            raise nexusrpc.HandlerError(str(exc), type=nexusrpc.HandlerErrorType.BAD_REQUEST) from exc
        if not input.url:
            raise nexusrpc.HandlerError("url is required", type=nexusrpc.HandlerErrorType.BAD_REQUEST)

        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        await handle.signal(ToolRegistryWorkflow.register_external, args=[agent_id, name, input.url])

    @nexusrpc.handler.sync_operation
    async def deregister(
        self, ctx: StartOperationContext, input: DeregisterInput
    ) -> None:
        """Remove one registration under one agent_id."""
        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        await handle.signal(ToolRegistryWorkflow.deregister, args=[input.agent_id, input.name])

    @nexusrpc.handler.sync_operation
    async def list_agent_entries(
        self, ctx: StartOperationContext, input: ListAgentEntriesInput
    ) -> ListAgentEntriesOutput:
        """Current 3rd-party tool lists (grouped by alias), for one agent_id. Tools are
        fetched live, every call."""
        agent_id = input.agent_id or ""
        if not agent_id:
            raise nexusrpc.HandlerError("agent_id is required", type=nexusrpc.HandlerErrorType.BAD_REQUEST)
        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        entries = await handle.query(ToolRegistryWorkflow.list_agent_entries, agent_id)
        remote_tools = await _fetch_tools_grouped(self._client, entries.remote_servers)
        # nex-gen wraps map-shaped (additionalProperties) fields in a named type instead
        # of a plain dict -- wrap _fetch_tools_grouped's plain dict/list-of-dicts here.
        return ListAgentEntriesOutput(
            remote_tools=ListAgentEntriesOutputRemoteTools(
                additional_properties={
                    alias: [
                        ListAgentEntriesOutputRemoteToolsValueItem(additional_properties=tool)
                        for tool in tools
                    ]
                    for alias, tools in remote_tools.items()
                }
            )
        )

    @nexusrpc.handler.sync_operation
    async def call_tool(
        self, ctx: StartOperationContext, input: CallToolInput
    ) -> CallToolOutput:
        """Invoke one tool via a standalone activity (Nexus + SAA, no backing workflow).
        Raises nexusrpc.HandlerError explicitly -- a bare exception defaults to UNKNOWN,
        which retries forever instead of surfacing to the caller."""
        agent_id = input.agent_id or ""
        alias = input.alias or ""
        name = input.name or ""
        if not agent_id or not alias:
            raise nexusrpc.HandlerError(
                "agent_id and alias are required", type=nexusrpc.HandlerErrorType.BAD_REQUEST
            )
        operation = name.removeprefix(f"{alias}_")
        if operation == name:
            raise nexusrpc.HandlerError(
                f"Tool name {name!r} does not start with alias {alias!r}",
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )

        handle = self._client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
        url: str | None = await handle.query(ToolRegistryWorkflow.find, args=[agent_id, alias])
        if url is None:
            raise nexusrpc.HandlerError(
                f"{alias!r} is not registered for agent {agent_id!r}.",
                type=nexusrpc.HandlerErrorType.NOT_FOUND,
            )

        # nex-gen wraps map-shaped (additionalProperties) fields in a named type instead
        # of a plain dict.
        arguments = input.arguments.additional_properties if input.arguments is not None else {}
        try:
            result = await self._client.execute_activity(
                mcp_proxy_activity,
                ExternalMCPCallInput(server_url=url, tool_name=operation, arguments=arguments),
                id=f"mcp-proxy-{uuid.uuid4()}",
                task_queue=temporalio.nexus.info().task_queue,
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=30),
                # No retries: the tool may not be idempotent (e.g. sends an email).
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityFailureError as exc:
            # Real RPC/transport failure. A tool-level error is data on `result` instead.
            raise nexusrpc.HandlerError(
                str(exc.cause or exc), type=nexusrpc.HandlerErrorType.INTERNAL, retryable_override=False
            ) from exc
        return CallToolOutput(
            result=CallToolOutputResult(
                additional_properties=result.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            )
        )
