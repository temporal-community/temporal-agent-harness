# ABOUTME: A2A wire types — a practical subset covering the methods this gateway implements
# (message/send, message/stream, tasks/get, tasks/cancel, tasks/resubscribe, AgentCard). No
# push notifications, file parts, or history pagination — see app.py.

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Message / Part / Artifact
# ---------------------------------------------------------------------------


class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str


class DataPart(BaseModel):
    """Escape hatch: send the handler's real input schema directly instead of plain text."""

    kind: Literal["data"] = "data"
    data: dict


Part = Annotated[Union[TextPart, DataPart], Field(discriminator="kind")]


class Message(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["message"] = "message"
    role: Literal["user", "agent"]
    parts: list[Part]
    message_id: str = Field(alias="messageId")
    task_id: str | None = Field(default=None, alias="taskId")
    context_id: str | None = Field(default=None, alias="contextId")


class Artifact(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    artifact_id: str = Field(alias="artifactId")
    name: str | None = None
    parts: list[Part] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

TaskState = Literal[
    "submitted", "working", "input-required", "completed", "canceled", "failed"
]


class TaskStatus(BaseModel):
    state: TaskState
    message: Message | None = None


class Task(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["task"] = "task"
    id: str
    context_id: str = Field(alias="contextId")
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)


class TaskStatusUpdateEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["status-update"] = "status-update"
    task_id: str = Field(alias="taskId")
    context_id: str = Field(alias="contextId")
    status: TaskStatus
    final: bool = False


class TaskArtifactUpdateEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["artifact-update"] = "artifact-update"
    task_id: str = Field(alias="taskId")
    context_id: str = Field(alias="contextId")
    artifact: Artifact
    append: bool = False
    last_chunk: bool = Field(default=True, alias="lastChunk")


StreamEvent = Union[Task, Message, TaskStatusUpdateEvent, TaskArtifactUpdateEvent]

# ---------------------------------------------------------------------------
# AgentCard
# ---------------------------------------------------------------------------


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class AgentCapabilities(BaseModel):
    streaming: bool = True
    push_notifications: bool = Field(default=False, alias="pushNotifications")

    model_config = ConfigDict(populate_by_name=True)


class AgentCard(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    protocol_version: str = Field(default="0.3.0", alias="protocolVersion")
    name: str
    description: str
    url: str
    version: str = "1.0.0"
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = Field(
        default_factory=lambda: ["text", "data"], alias="defaultInputModes"
    )
    default_output_modes: list[str] = Field(
        default_factory=lambda: ["text", "data"], alias="defaultOutputModes"
    )
    skills: list[AgentSkill] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# JSON-RPC envelope
# ---------------------------------------------------------------------------


class JSONRPCRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    method: str
    params: dict = Field(default_factory=dict)


class JSONRPCError(BaseModel):
    code: int
    message: str
    data: dict | None = None


class JSONRPCErrorResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    error: JSONRPCError


class JSONRPCSuccessResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: dict


# Standard JSON-RPC / A2A error codes this gateway raises.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002


# ---------------------------------------------------------------------------
# Method param shapes
# ---------------------------------------------------------------------------


class MessageSendConfiguration(BaseModel):
    blocking: bool = False


class MessageSendParams(BaseModel):
    message: Message
    configuration: MessageSendConfiguration | None = None


class TaskIdParams(BaseModel):
    id: str
