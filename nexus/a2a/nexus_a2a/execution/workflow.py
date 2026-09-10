"""Execute Nexus-backed A2A operations inside a Temporal workflow."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

from nexus_a2a.context import RequestContext


class WorkflowNexusExecutor:
    """Execute Nexus operations using the current workflow context."""

    async def execute(
        self,
        *,
        service: type[Any] | str,
        endpoint: str,
        operation: Any,
        argument: Any,
        context: RequestContext,
    ) -> Any:
        client = workflow.create_nexus_client(service=service, endpoint=endpoint)
        options: dict[str, Any] = {"headers": context.nexus_headers or None}
        if context.timeout_seconds is not None:
            options["schedule_to_close_timeout"] = timedelta(
                seconds=context.timeout_seconds
            )
        return await client.execute_operation(operation, argument, **options)
