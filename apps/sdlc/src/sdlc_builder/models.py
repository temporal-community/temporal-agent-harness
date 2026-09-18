"""Small, versioned application contracts shared by workflow, API, and activities."""

from typing import Literal

from pydantic import BaseModel, Field
from temporal_agent_harness.harness.state import HarnessState


class Profile(BaseModel):
    id: str
    label: str
    provider: Literal["demo", "openai", "anthropic", "compatible"]
    model: str = ""
    base_url: str = ""
    credential: str = ""  # env:NAME or keyring:opaque-id; never the key itself
    max_steps: int = Field(default=24, ge=3, le=200)


class TaskInput(BaseModel):
    """One task, its isolated workspace identity, and immutable provider settings."""

    task_id: str
    prompt: str = Field(min_length=1, max_length=20000)
    mode: Literal["ask", "change"] = "change"
    profile: Profile
    continuation_context: str = Field(default="", max_length=8000)


class FollowUpInput(BaseModel):
    """A new user message and its immutable execution settings in an existing task."""

    message_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    prompt: str = Field(min_length=1, max_length=20000)
    mode: Literal["ask", "change"]
    profile: Profile


class Action(BaseModel):
    kind: Literal[
        "list", "read", "search", "plan", "write", "check", "question", "finish"
    ]
    summary: str = Field(
        description="Short public explanation of the next action, not private reasoning."
    )
    path: str = ""
    content: str = ""
    expected_hash: str = ""  # empty means file must not exist
    query: str = ""
    argv: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    completed_tasks: list[int] = Field(
        default_factory=list,
        description="Zero-based plan task indices completed after this action succeeds",
    )
    active_task: int | None = Field(
        default=None, description="Zero-based plan task index currently being worked on"
    )


class ModelRequest(BaseModel):
    task: TaskInput
    context: list[dict[str, str]]
    step: int


class ModelResult(BaseModel):
    action: Action
    input_tokens: int = 0
    output_tokens: int = 0


class Step(HarnessState):
    id: str
    title: str
    status: str = "pending"


class Entry(HarnessState):
    id: str
    role: str
    text: str


class Check(HarnessState):
    id: str
    command: str
    exit_code: int
    output: str
    stale: bool = False


class Gate(HarnessState):
    id: str
    kind: str
    title: str
    detail: str


class TaskFailure(HarnessState):
    category: str
    title: str
    explanation: str
    actions: list[str]
    http_status: int | None = None
    provider_code: str = ""


class SdlcStateV1(HarnessState):
    schema_version: int = 1
    task_id: str = ""
    mode: str = "change"
    phase: str = "queued"
    status: str = "queued"
    focus: str = "Waiting for the worker"
    next_action: str = "Inspect the repository"
    steps: list[Step] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    gate: Gate | None = None
    entries: list[Entry] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    model: str = ""
    model_calls: int = 0
    max_steps: int = 24
    input_tokens: int = 0
    output_tokens: int = 0
    outcome: str = ""


class SdlcState(SdlcStateV1):
    schema_version: int = 2
    failure: TaskFailure | None = None


class ConversationTurn(HarnessState):
    number: int
    message_id: str
    prompt: str
    mode: str
    profile_id: str
    profile_label: str
    provider: str
    model: str
    max_steps: int
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    status: str = "running"
    outcome: str = ""


class ConversationState(HarnessState):
    turns: list[ConversationTurn] = Field(default_factory=list)
    context_shortened: bool = False


class Control(BaseModel):
    command: Literal[
        "pause",
        "resume",
        "cancel",
        "respond",
        "accept",
        "edit_started",
        "edit_finished",
    ]
    gate_id: str = ""
    approved: bool = True
    text: str = Field(default="", max_length=20000)
