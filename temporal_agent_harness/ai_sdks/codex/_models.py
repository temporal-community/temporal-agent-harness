"""Codex contracts; worker configuration never crosses the workflow boundary."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from temporal_agent_harness.harness.agent_protocol import TokenUsage
from temporal_agent_harness.harness.stream_context import TurnStreamContext


ACTIVITY_NAME = "codex_execute"


class CodexConfig(BaseModel):
    """Trusted worker settings for the installed Codex CLI.

    Internal Codex tools use this sandbox; they are not dispatched through the
    harness's per-tool approval gate. Use a dedicated workspace for write access.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace: str | Path
    codex_binary: str = "codex"
    sandbox: Literal["read-only", "workspace-write"] = "read-only"
    model: str | None = None
    state_dir: str | Path = ".codex-runs"
    timeout_seconds: float = Field(default=600, ge=0.1, le=3600)


class CodexResult(BaseModel):
    """The final Codex reply, reported token usage, and local diagnostic location."""

    text: str
    thread_id: str | None = None
    usage: TokenUsage | None = None
    log_path: str | None = None


class CodexRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=200_000)
    invocation_id: str
    stream_context: TurnStreamContext
