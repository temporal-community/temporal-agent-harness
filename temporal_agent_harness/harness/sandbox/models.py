# ABOUTME: Workflow-safe request/result models + activity name constants for the sandbox
# lifecycle activities (activate/pause/terminate). Mirrors
# `harness/code_mode/batch_models.py`'s split exactly: this module imports ONLY `pydantic` +
# the dependency-free `sandbox_ref` leaf module — never `remote-box` — so
# `agent_workflow.py`'s `AgentWorkflowRunner` can dispatch these activities BY NAME, from
# pure workflow code, without dragging the sandbox engine (remote-box, and the Daytona/E2B SDKs
# it depends on) into the deterministic workflow's own execution context. `harness/sandbox/
# activities.py` (worker-side, imports remote-box) reconstructs the typed backend config from
# `backend` via `backend_from_dict`.
#
# No `from __future__ import annotations` — these cross Temporal's pydantic converter; see
# `harness/code_mode/batch_models.py`'s identical note.

from typing import Any

from pydantic import BaseModel

from temporal_agent_harness.harness.sandbox_ref import SandboxRef

SANDBOX_ACTIVATE_ACTIVITY = "sandbox_activate"
SANDBOX_PAUSE_ACTIVITY = "sandbox_pause"
SANDBOX_TERMINATE_ACTIVITY = "sandbox_terminate"


class SandboxActivateInput(BaseModel):
    ref: SandboxRef | None = None
    backend: dict[str, Any]
    local_project_root: str
    require_prebuilt: bool = True


class SandboxPauseInput(BaseModel):
    ref: SandboxRef
    backend: dict[str, Any]
    local_project_root: str


class SandboxTerminateInput(BaseModel):
    ref: SandboxRef
    backend: dict[str, Any]
    local_project_root: str


class SandboxRefResult(BaseModel):
    ref: SandboxRef
