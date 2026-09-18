"""Portable V2 role contracts and observable, evidence-backed lifecycle state."""

from typing import Literal

from pydantic import BaseModel, Field
from temporal_agent_harness.harness.state import HarnessState

from .models import Check, Profile, SdlcState, TaskFailure


class FileRequest(BaseModel):
    """Read one text file in the assigned workspace."""

    path: str


class SearchRequest(BaseModel):
    """Find literal text in repository files."""

    query: str = Field(min_length=1, max_length=500)


class FileChange(BaseModel):
    """An exact-hash file replacement; an empty hash creates a new file."""

    path: str
    expected_hash: str
    content: str = Field(max_length=100000)


class PatchRequest(BaseModel):
    """Preflight all replacements before applying a bounded, recoverable patch."""

    changes: list[FileChange] = Field(min_length=1, max_length=20)


class EditRequest(BaseModel):
    """Replace exactly one occurrence in a file read at expected_hash."""

    path: str
    expected_hash: str
    old_text: str = Field(min_length=1, max_length=100000)
    new_text: str = Field(max_length=100000)


class Recipe(HarnessState):
    """A proposed check, always executed at workspace root after human approval."""

    title: str
    argv: list[str] = Field(min_length=1, max_length=50)


class Finding(HarnessState):
    severity: Literal["high", "medium", "low"]
    path: str
    line: int = 0
    explanation: str


class RoleOutput(BaseModel):
    """A role's proposal or assessment, never an authorization or execution receipt."""

    summary: str
    requirements: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)
    checks: list[Recipe] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class Blueprint(HarnessState):
    revision: int
    request: str
    summary: str
    requirements: list[str]
    assumptions: list[str]
    scope: list[str]
    checks: list[Recipe]
    approved: bool = False
    approval_note: str = ""


class FileView(BaseModel):
    path: str
    exists: bool
    hash: str
    content: str


class SearchHit(BaseModel):
    path: str
    line: int
    text: str


class ToolResult(BaseModel):
    """Durable host receipt; produced only by the controller and workspace activity."""

    ok: bool = True
    error: str = ""
    operation_id: str = ""
    files: list[str] = Field(default_factory=list)
    file: FileView | None = None
    matches: list[SearchHit] = Field(default_factory=list)
    patch: str = ""
    revision: str = ""
    before_revision: str = ""
    exit_code: int | None = None
    output: str = ""


class ToolCall(BaseModel):
    """Trusted dispatch envelope, hidden from model tool schemas."""

    kind: Literal[
        "list",
        "read",
        "search",
        "diff",
        "revision",
        "patch",
        "edit",
        "check",
        "model",
        "usage",
    ]
    path: str = ""
    query: str = ""
    changes: list[FileChange] = Field(default_factory=list)
    edit: EditRequest | None = None
    argv: list[str] = Field(default_factory=list)
    expected_revision: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class ProjectMergeReceipt(HarnessState):
    """Durable workflow receipt for a project working-tree merge."""

    ok: bool = True
    operation_id: str
    base_revision: str
    revision: str
    files: list[str] = Field(default_factory=list)
    applied_files: list[str] = Field(default_factory=list)
    already_present_files: list[str] = Field(default_factory=list)
    already_applied: bool = False
    project_path: str
    branch: str
    merged_at: float


class ProjectMergeResult(BaseModel):
    """Activity result kept separate from lifecycle authorization."""

    ok: bool = True
    error: str = ""
    receipt: ProjectMergeReceipt | None = None


class HostRequest(BaseModel):
    """A child request with assignment and deterministic request identity."""

    assignment_id: str
    request_id: str
    caller: str
    call: ToolCall


class HostReply(BaseModel):
    """Controller response to a child request."""

    request_id: str
    result: ToolResult


class Assignment(BaseModel):
    """Controller-owned scope for one child turn; no model can grant this scope."""

    id: str
    parent: str
    role: Literal["implementer", "reviewer"]
    profile: Profile
    prompt: str
    max_calls: int


class RoleReply(BaseModel):
    """The settled child result, with failures kept separate from completion."""

    output: RoleOutput | None = None
    error: str = ""
    failure: TaskFailure | None = None


class Actor(HarnessState):
    id: str
    role: str
    status: str = "running"
    operation: str = "Starting"
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    host_calls: int = 0


class Evidence(Check):
    revision: str = ""
    after_revision: str = ""


class CodingState(SdlcState):
    schema_version: int = 4
    engine: str = "v2"
    blueprints: list[Blueprint] = Field(default_factory=list)
    actors: list[Actor] = Field(default_factory=list)
    revision: str = ""
    review_revision: str = ""
    findings: list[Finding] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    verification: str = "not_run"
    repair_attempt: int = 0
    host_calls: int = 0
    checks: list[Evidence] = Field(default_factory=list)
    acceptance_note: str = ""
    project_merge: ProjectMergeReceipt | None = None
