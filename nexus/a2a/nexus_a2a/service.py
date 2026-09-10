"""A2A's request model bound to Temporal Nexus."""

from __future__ import annotations

import nexusrpc
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
from pydantic import BaseModel, Field

A2A_NEXUS_BINDING = "https://temporal.io/a2a/bindings/nexus"
A2A_PROTOCOL_VERSION = "1.0"
A2A_SERVICE_NAME = "A2AService"


class SubscribeToTaskInput(BaseModel):
    """A bounded A2A subscription request for the Nexus protocol binding."""

    tenant: str = ""
    id: str
    cursor: int = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=30.0, gt=0, le=60)


class SubscribeToTaskItem(BaseModel):
    """One serialized ``lf.a2a.v1.StreamResponse`` and its durable cursor."""

    offset: int = Field(ge=0)
    data: str


class SubscribeToTaskOutput(BaseModel):
    """One bounded page from an A2A task subscription."""

    items: list[SubscribeToTaskItem]
    next_cursor: int = Field(ge=0)
    more_ready: bool = False
    closed: bool = False


@nexusrpc.service(name=A2A_SERVICE_NAME)
class A2AService:
    """A2A v1 methods exposed through a Temporal Nexus endpoint."""

    send_message = nexusrpc.Operation(
        "SendMessage", SendMessageRequest, SendMessageResponse
    )
    get_task = nexusrpc.Operation("GetTask", GetTaskRequest, Task)
    list_tasks = nexusrpc.Operation("ListTasks", ListTasksRequest, ListTasksResponse)
    cancel_task = nexusrpc.Operation("CancelTask", CancelTaskRequest, Task)
    subscribe_to_task = nexusrpc.Operation(
        "SubscribeToTask", SubscribeToTaskInput, SubscribeToTaskOutput
    )
    get_extended_agent_card = nexusrpc.Operation(
        "GetExtendedAgentCard", GetExtendedAgentCardRequest, AgentCard
    )
