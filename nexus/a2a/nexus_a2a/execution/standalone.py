"""Execute Nexus-backed A2A operations outside a Temporal workflow."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from temporalio.client import Client
from temporalio.common import NexusOperationIDConflictPolicy

from nexus_a2a.context import RequestContext


class StandaloneNexusExecutor:
    """Execute standalone Nexus operations through a normal Temporal client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def execute(
        self,
        *,
        service: type[Any] | str,
        endpoint: str,
        operation: Any,
        argument: Any,
        context: RequestContext,
    ) -> Any:
        client = self._client.create_nexus_client(service=service, endpoint=endpoint)
        operation_id = context.idempotency_key or f"a2a-{uuid.uuid4()}"
        return await client.execute_operation(
            operation,
            argument,
            id=operation_id,
            id_conflict_policy=(
                NexusOperationIDConflictPolicy.USE_EXISTING
                if context.idempotency_key
                else NexusOperationIDConflictPolicy.FAIL
            ),
            headers=context.nexus_headers or None,
            schedule_to_close_timeout=(
                timedelta(seconds=context.timeout_seconds)
                if context.timeout_seconds is not None
                else None
            ),
        )
