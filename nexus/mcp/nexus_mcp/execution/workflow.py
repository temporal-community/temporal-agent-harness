"""Executor that allows us to execute Nexus-backed MCP operations from within a Temporal workflow."""

from __future__ import annotations

from typing import Any

from temporalio import workflow

from nexus_mcp.resolver import RequestContext


class WorkflowNexusExecutor:
    """Execute Nexus operations using the current workflow context."""

    async def execute(
        self,
        *,
        service: str,
        endpoint: str,
        operation: str,
        argument: Any,
        context: RequestContext,
    ) -> Any:
        client = workflow.create_nexus_client(service=service, endpoint=endpoint)
        return await client.execute_operation(
            operation,
            argument,
            headers=context.nexus_headers or None,
        )
