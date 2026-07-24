"""ToolCallWorkflow - durable wrapper for every MCP tool call the HTTP-facing
Durable Tool Call Gateway (``server.py``, ``InboundGateway``) serves.

Every tool call request that gateway receives is executed as a Temporal
workflow with a content-hash stable ID.  This gives three crash-safety
properties simultaneously:

  1. MCP server crashes mid-execution
       The workflow retries the activity according to its retry policy,
       without the gateway or client knowing.

  2. Gateway crashes after tool executes but before response is sent
       The workflow completed and its result is in Temporal history.  When the
       client retries, start_workflow(id=same_hash, USE_EXISTING) returns a
       handle to the *already-completed* workflow; handle.result() reads the
       cached result instantly.

  3. HTTP disconnect (client drops the connection mid-call)
       The workflow keeps running in Temporal regardless of the HTTP layer.
       When the client reconnects and retries, same hash -> same workflow -> result.

``RegistryServiceHandler.call_tool`` (this same package's ``registry_service_handler``)
starts this SAME workflow for its own callers too — i.e. WorkflowTransport, reaching the
gateway via Nexus. An earlier version dispatched ``mcp_proxy_activity`` there as a
standalone activity instead, on the theory that a caller that's already a durable Temporal
workflow doesn't need a sub-workflow's crash-safety on top of its own — but standalone
activities need an experimental server capability (``nexusoperation.enableStandalone``)
that's been observed to deadlock the CALLING workflow in real usage. Going through this
workflow either way is one extra, cheap Temporal hop for a meaningfully more battle-tested
path.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .activities import ExternalMCPCallInput, mcp_proxy_activity

logger = logging.getLogger(__name__)


@workflow.defn(name="ToolCall", sandboxed=False)
class ToolCallWorkflow:
    """Execute one MCP tool call durably inside a Temporal workflow."""

    @workflow.run
    async def run(self, input: ExternalMCPCallInput) -> str:
        return await workflow.execute_activity(
            mcp_proxy_activity,
            input,
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=3,
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
            ),
        )
