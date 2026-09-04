"""Executor that allows us to execute Nexus-backed MCP operations from outside a Temporal workflow."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from temporalio.client import Client, NexusOperationFailureError
from temporalio.common import (
    NexusOperationExecutionStatus,
    NexusOperationIDConflictPolicy,
)

from nexus_mcp.resolver import NexusTask, RequestContext


class StandaloneNexusExecutor:
    """Execute standalone Nexus operations through a normal Temporal client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def execute(
        self,
        *,
        service: str,
        endpoint: str,
        operation: str,
        argument: Any,
        context: RequestContext,
    ) -> Any:
        client = self._client.create_nexus_client(service=service, endpoint=endpoint)
        operation_id = context.idempotency_key or str(uuid.uuid4())
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
        )

    async def start_task(
        self,
        *,
        service: str,
        endpoint: str,
        operation: str,
        argument: Any,
        context: RequestContext,
    ) -> NexusTask:
        """Start a standalone Nexus operation and return its durable task state."""
        client = self._client.create_nexus_client(service=service, endpoint=endpoint)
        operation_id = context.idempotency_key or str(uuid.uuid4())
        handle = await client.start_operation(
            operation,
            argument,
            id=operation_id,
            id_conflict_policy=(
                NexusOperationIDConflictPolicy.USE_EXISTING
                if context.idempotency_key
                else NexusOperationIDConflictPolicy.FAIL
            ),
            headers=context.nexus_headers or None,
        )
        return await self._task_from_handle(handle)

    async def get_task(self, task_id: str) -> NexusTask:
        """Read a standalone Nexus operation by its durable operation ID."""
        return await self._task_from_handle(
            self._client.get_nexus_operation_handle(task_id)
        )

    async def cancel_task(self, task_id: str) -> None:
        """Request cancellation of a standalone Nexus operation."""
        await self._client.get_nexus_operation_handle(task_id).cancel(
            reason="MCP client requested task cancellation"
        )

    @staticmethod
    async def _task_from_handle(handle: Any) -> NexusTask:
        description = await handle.describe()
        now = datetime.now(UTC)
        created_at = description.schedule_time or now
        last_updated_at = (
            description.close_time
            or description.last_attempt_complete_time
            or created_at
        )
        status = description.status

        if status == NexusOperationExecutionStatus.RUNNING:
            return NexusTask(
                task_id=handle.operation_id,
                service=description.service,
                endpoint=description.endpoint,
                operation=description.operation,
                status="working",
                created_at=created_at,
                last_updated_at=last_updated_at,
            )
        if status == NexusOperationExecutionStatus.COMPLETED:
            return NexusTask(
                task_id=handle.operation_id,
                service=description.service,
                endpoint=description.endpoint,
                operation=description.operation,
                status="completed",
                created_at=created_at,
                last_updated_at=last_updated_at,
                result=await handle.result(),
            )
        if status == NexusOperationExecutionStatus.CANCELED:
            return NexusTask(
                task_id=handle.operation_id,
                service=description.service,
                endpoint=description.endpoint,
                operation=description.operation,
                status="cancelled",
                created_at=created_at,
                last_updated_at=last_updated_at,
            )

        error = description.last_attempt_failure
        if error is None:
            try:
                await handle.result()
            except NexusOperationFailureError as exc:
                error = exc.cause
            except Exception as exc:  # noqa: BLE001
                error = exc
        return NexusTask(
            task_id=handle.operation_id,
            service=description.service,
            endpoint=description.endpoint,
            operation=description.operation,
            status="failed",
            created_at=created_at,
            last_updated_at=last_updated_at,
            error=str(error or f"Nexus operation ended with status {status.name}"),
        )
