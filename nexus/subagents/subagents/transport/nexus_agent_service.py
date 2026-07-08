# ABOUTME: Python mirror of the Go SubagentService Nexus contract (subagent_adapter). Field
# names/casing must match the Go wire types byte-for-byte. Sandbox-safe (stdlib+pydantic+nexusrpc).

from __future__ import annotations

import nexusrpc
from pydantic import BaseModel, ConfigDict, Field


class Part(BaseModel):
    """'data' parts JSON-encode {handler, input} — our dispatch convention, not standard A2A."""

    kind: str  # "text" | "data"
    text: str | None = None
    data: str | None = None


class Message(BaseModel):
    """parts[0] is always a 'data' part carrying {"handler": name, "input": {...}}."""

    model_config = ConfigDict(populate_by_name=True)

    role: str  # "user" | "agent"
    parts: list[Part]
    task_id: str = Field(alias="taskId")
    message_id: str = Field(default="", alias="messageId")


class TaskStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    state: str  # submitted|working|input-required|completed|failed|canceled
    message: Message | None = None


class Artifact(BaseModel):
    name: str
    parts: list[Part]


class Task(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    context_id: str = Field(alias="contextId")
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    stream_head_offset: int = Field(default=0, alias="streamHeadOffset")
    # Harness extension, not standard A2A. Only set on sendMessage's response.
    turn_number: int = Field(default=0, alias="turnNumber")


class SendMessageInput(BaseModel):
    message: Message


class GetTaskInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="taskId")


class PollTaskUpdatesInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="taskId")
    cursor: int
    timeout_seconds: float | None = Field(default=None, alias="timeoutSeconds")


class StreamItem(BaseModel):
    """One item from PollTaskUpdatesOutput.items — base64(proto Payload{...AgentEvent JSON})."""

    topic: str
    data: str
    offset: int


class PollTaskUpdatesOutput(BaseModel):
    """No task_state field: the caller derives it by decoding items (see turn_driver.py)."""

    items: list[StreamItem] = Field(default_factory=list)
    next_offset: int = 0
    more_ready: bool = False
    closed: bool = False


class CancelTaskInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="taskId")


@nexusrpc.service
class SubagentService:
    """Python-side reference to the Go SubagentService Nexus service (subagent_adapter)."""

    send_message: nexusrpc.Operation[SendMessageInput, Task] = nexusrpc.Operation(
        name="sendMessage",
        input_type=SendMessageInput,
        output_type=Task,
    )
    get_task: nexusrpc.Operation[GetTaskInput, Task] = nexusrpc.Operation(
        name="getTask",
        input_type=GetTaskInput,
        output_type=Task,
    )
    poll_task_updates: nexusrpc.Operation[PollTaskUpdatesInput, PollTaskUpdatesOutput] = (
        nexusrpc.Operation(
            name="pollTaskUpdates",
            input_type=PollTaskUpdatesInput,
            output_type=PollTaskUpdatesOutput,
        )
    )
    cancel_task: nexusrpc.Operation[CancelTaskInput, Task] = nexusrpc.Operation(
        name="cancelTask",
        input_type=CancelTaskInput,
        output_type=Task,
    )
