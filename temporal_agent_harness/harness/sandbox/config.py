# ABOUTME: SandboxConfig — the single place an agent definition picks which sandbox backend
# its `sandboxed=True` tools run in. Requires the optional `sandbox` extra (microsandbox);
# importing this module (and thus `temporal_agent_harness.harness.sandbox`) is how an agent
# author opts into that dependency — core harness code (`agent_workflow.py`) never imports it.

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from temporalio.workflow import ActivityConfig


class MicrosandboxBackend(BaseModel):
    """microsandbox microVM backend — harness-owned config, no remote-box types."""

    type: Literal["microsandbox"] = "microsandbox"
    snapshot_name: str | None = None
    image: str | None = None
    dockerfile_path: str | None = None
    cpus: int | None = None
    memory: int | None = Field(default=None, description="Memory limit in MiB.")
    network: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None
    env: dict[str, str] | None = None


BackendProvider = Callable[[], Awaitable[MicrosandboxBackend | Literal["local"]]]
"""An async producer of a backend config for a :class:`SandboxConfig` that can't state one up front."""


class SandboxConfig(BaseModel):
    """Which sandbox backend an agent's ``sandboxed=True`` tools run in, and how strictly.

    Pass once to ``AgentWorkflowRunner(..., sandbox=SandboxConfig(...))`` — the single place
    backend choice is made for a given agent. A ``sandboxed=True`` tool never chooses its own
    backend, so the same tool is reusable across agents with zero code changes.

    ``backend="local"`` runs tool bodies in-process on the worker (no microVM) — for CI and unit
    tests. Production agents use :class:`MicrosandboxBackend` (from_snapshot) or a provider name.

    Construct from inside an agent's ``@workflow.init`` under
    ``with workflow.unsafe.imports_passed_through():`` when importing harness sandbox types.
    """

    backend: Literal["local"] | MicrosandboxBackend | str
    """``"local"`` for in-process CI, a :class:`MicrosandboxBackend`, or a provider name."""

    local_project_root: Path

    require_prebuilt: bool = True
    """Runtime activation NEVER builds a missing snapshot when True (default). Build ahead of time
    with ``build_sandbox(config)`` from CI."""

    activity_config: ActivityConfig | None = None
    """Applies to all three sandbox lifecycle activities (activate/pause/terminate)."""
