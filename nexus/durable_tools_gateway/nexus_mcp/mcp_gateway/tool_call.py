"""ToolCallWorkflow - durable wrapper for every MCP tool call.

Every tool call request the gateway receives is executed as a Temporal
workflow with a content-hash stable ID.  This gives three crash-safety
properties simultaneously:

  1. MCP server crashes mid-execution
       The workflow retries the Nexus call (1st-party) or the activity (3rd-party)
       according to its retry policy, without the gateway or client knowing.

  2. Gateway crashes after tool executes but before response is sent
       The workflow completed and its result is in Temporal history.  When the
       client retries, start_workflow(id=same_hash, USE_EXISTING) returns a
       handle to the *already-completed* workflow; handle.result() reads the
       cached result instantly.

  3. HTTP disconnect (client drops the connection mid-call)
       The workflow keeps running in Temporal regardless of the HTTP layer.
       When the client reconnects and retries, same hash -> same workflow -> result.

Routing:
  kind=NEXUS    -> workflow.create_nexus_client().execute_operation()
                    Routes to a 1st-party @sync_operation handler on its
                    dedicated Nexus endpoint.

  kind=EXTERNAL -> workflow.execute_activity(mcp_proxy_activity, ...)
                    Makes an outbound HTTP call to a 3rd-party MCP server.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from enum import Enum
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)


# -- enums / models -----------------------------------------------------------


class MCPServerKind(Enum):
    NEXUS = 1     # 1st-party (nexus-backed) MCP servers
    EXTERNAL = 2  # 3rd-party MCP servers reached over HTTP


class ExternalMCPCallInput(BaseModel):
    """Input for an HTTP call to a 3rd-party MCP server."""

    server_url: str
    tool_name: str
    arguments: dict[str, Any]


class ToolCallWorkflowInput(BaseModel):
    """Input for a single MCP tool call, routed through ToolCallWorkflow."""

    kind: MCPServerKind

    # shared
    arguments: dict[str, Any] = {}

    # nexus only
    service: str = ""
    operation: str = ""
    endpoint: str = ""

    # external only
    server_url: str = ""
    tool_name: str = ""

# -- workflow (nexus + external) ----------------------------------------------


def _result_to_str(result: Any) -> str:
    if isinstance(result, str):
        return result
    if hasattr(result, "model_dump_json"):
        return result.model_dump_json(indent=2)
    return str(result)


@workflow.defn(name="ToolCall", sandboxed=False)
class ToolCallWorkflow:
    """Execute one MCP tool call durably inside a Temporal workflow."""

    @workflow.run
    async def run(self, input: ToolCallWorkflowInput) -> str:
        if input.kind == MCPServerKind.NEXUS:
            nexus_client = workflow.create_nexus_client(
                service=input.service,
                endpoint=input.endpoint,
            )
            result = await nexus_client.execute_operation(
                input.operation,
                input.arguments,
            )
            return _result_to_str(result)

        return await workflow.execute_activity(
            mcp_proxy_activity,
            ExternalMCPCallInput(
                server_url=input.server_url,
                tool_name=input.tool_name,
                arguments=input.arguments,
            ),
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
            ),
        )


# -- activity (external path) -------------------------------------------------


@activity.defn
async def mcp_proxy_activity(input: ExternalMCPCallInput) -> str:
    """Call a tool on an external MCP server over Streamable HTTP."""
    activity.logger.info(
        "[proxy-activity] calling %r on %s", input.tool_name, input.server_url
    )
    activity.heartbeat()

    async with streamable_http_client(input.server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(input.tool_name, input.arguments)

    activity.logger.info(
        "[proxy-activity] %r completed  is_error=%s", input.tool_name, result.isError
    )
    return _call_tool_result_to_str(result)


def _call_tool_result_to_str(result: Any) -> str:
    if result.isError:
        texts = [
            c.text for c in (result.content or []) if hasattr(c, "text") and c.text
        ]
        raise RuntimeError(texts[0] if texts else "MCP tool returned an error")
    parts: list[str] = []
    for c in result.content or []:
        if hasattr(c, "text") and c.text:
            parts.append(c.text)
        elif hasattr(c, "data"):
            parts.append(f"[binary data: {len(c.data)} bytes]")
        else:
            parts.append(str(c))
    return "\n".join(parts)