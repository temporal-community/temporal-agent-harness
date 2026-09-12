# ABOUTME: Argument/result models for the sandbox activities, plus the activity-name scheme that
# lets several backends coexist on one task queue. One typed model per activity (rather than
# positional args) so the pydantic data converter round-trips everything, and so adding a field
# later is not a signature change.
#
# Only ``SandboxState`` and these small models ever cross the activity boundary — workspace
# contents do not, unless a caller explicitly uses read/write. See protocol.SupportsHydration.
#
# No ``from __future__ import annotations``: these cross Temporal's pydantic data converter, which
# needs concrete annotations.

from base64 import b64decode, b64encode
from typing import Annotated, Any, Dict, List, Optional

from pydantic import BaseModel, BeforeValidator, PlainSerializer

from temporal_agent_harness.harness.sandbox.protocol import ExecResult, FsEntry, SandboxState


def _coerce_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return b64decode(value)
    raise ValueError(f"expected bytes or a base64 string, got {type(value)}")


# Raw bytes in Python, base64 on the wire — lossless for arbitrary binary content under a JSON
# payload converter.
JsonSafeBytes = Annotated[
    bytes,
    BeforeValidator(_coerce_bytes),
    PlainSerializer(lambda v: b64encode(v).decode("ascii"), return_type=str),
]


# ---------------------------------------------------------------------------
# Activity naming
# ---------------------------------------------------------------------------

# Every activity is prefixed with its provider's name, so a worker can host several sandbox
# backends on one task queue and a workflow selects between them by name alone.
CREATE = "sandbox_create"
RESUME = "sandbox_resume"
DELETE = "sandbox_delete"
EXEC = "sandbox_exec"
RUN_CODE = "sandbox_run_code"
READ = "sandbox_read"
WRITE = "sandbox_write"
LS = "sandbox_ls"
RUNNING = "sandbox_running"
HYDRATE = "sandbox_hydrate"
PERSIST = "sandbox_persist"


def activity_name(provider: str, operation: str) -> str:
    """The Temporal activity name for ``operation`` on the provider called ``provider``."""
    return f"{provider}-{operation}"


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


class CreateArgs(BaseModel):
    """Creation options as a plain mapping; the provider validates them into the backend's
    ``options_model`` worker-side, so a subclass survives the trip without discriminators."""

    options: Dict[str, Any] = {}


class StateArgs(BaseModel):
    """For operations that need only the sandbox identity (resume / delete / running)."""

    state: SandboxState


class ExecArgs(BaseModel):
    state: SandboxState
    command: List[str]
    timeout: Optional[float] = None


class RunCodeArgs(BaseModel):
    state: SandboxState
    code: str
    language: str = "python"
    timeout: Optional[float] = None


class ReadArgs(BaseModel):
    state: SandboxState
    path: str


class WriteArgs(BaseModel):
    state: SandboxState
    files: Dict[str, JsonSafeBytes]


class LsArgs(BaseModel):
    state: SandboxState
    path: str = "."
    depth: int = 1


class LocatorArgs(BaseModel):
    """For hydrate / persist. ``locator=None`` means "derive it from the sandbox's own identity"."""

    state: SandboxState
    locator: Optional[str] = None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class ReadResult(BaseModel):
    data: JsonSafeBytes


class WriteResult(BaseModel):
    bytes_written: int = 0


class LsResult(BaseModel):
    entries: List[FsEntry] = []


class RunningResult(BaseModel):
    is_running: bool = False


class HydrateResult(BaseModel):
    files_written: int = 0


class PersistResult(BaseModel):
    locator: str = ""


__all__ = [
    "CREATE",
    "DELETE",
    "EXEC",
    "HYDRATE",
    "LS",
    "PERSIST",
    "READ",
    "RESUME",
    "RUNNING",
    "RUN_CODE",
    "WRITE",
    "CreateArgs",
    "ExecArgs",
    "ExecResult",
    "HydrateResult",
    "JsonSafeBytes",
    "LocatorArgs",
    "LsArgs",
    "LsResult",
    "PersistResult",
    "ReadArgs",
    "ReadResult",
    "RunCodeArgs",
    "RunningResult",
    "StateArgs",
    "WriteArgs",
    "WriteResult",
    "activity_name",
]
