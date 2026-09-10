"""Runtime-independent backend contract for Nexus A2A services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetExtendedAgentCardRequest,
    GetTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    SendMessageRequest,
    SendMessageResponse,
    Task,
)
from temporalio import nexus

from nexus_a2a.service import SubscribeToTaskInput, SubscribeToTaskOutput


@dataclass(frozen=True)
class OperationContext:
    """Nexus request metadata passed intact to an A2A backend."""

    request_id: str
    service: str
    operation: str
    headers: Mapping[str, str] = field(default_factory=dict)
    request_deadline: datetime | None = None


class A2ABackend(Protocol):
    """Implement A2A task semantics independently of the hosting agent runtime."""

    async def send_message(
        self, context: OperationContext, request: SendMessageRequest
    ) -> SendMessageResponse: ...

    async def get_task(
        self, context: OperationContext, request: GetTaskRequest
    ) -> Task: ...

    async def list_tasks(
        self, context: OperationContext, request: ListTasksRequest
    ) -> ListTasksResponse: ...

    async def cancel_task(
        self, context: OperationContext, request: CancelTaskRequest
    ) -> Task: ...

    async def get_extended_agent_card(
        self, context: OperationContext, request: GetExtendedAgentCardRequest
    ) -> AgentCard: ...

    async def subscribe_to_task(
        self,
        context: OperationContext,
        client: nexus.TemporalNexusClient,
        request: SubscribeToTaskInput,
    ) -> nexus.TemporalOperationResult[SubscribeToTaskOutput]: ...
