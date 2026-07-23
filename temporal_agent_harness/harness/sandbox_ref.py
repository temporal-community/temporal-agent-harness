# ABOUTME: Internal harness model for a serializable pointer to a live sandboxed-tool sandbox.
# Kept in its own leaf module (stdlib + pydantic only, importing nothing else from ``harness``,
# and NOT even the optional ``remote-box`` package) — mirrors ``stream_context.py``'s role, so
# BOTH the workflow-side runner (``harness/agent_workflow.py``) and the worker-side sandbox
# activities (``harness/sandbox/activities.py``, which DOES depend on remote-box) can share one
# wire type without core harness code ever needing the optional ``sandbox`` extra installed.
#
# Mirrors ``remote.SessionRef``'s shape field-for-field on purpose (``backend: str``,
# ``sandbox_id: str | None``) — ``harness/sandbox/activities.py`` converts between the two at the
# boundary where it talks to remote-box directly.
#
# No ``from __future__ import annotations`` — this crosses Temporal's pydantic converter as part
# of ``AgentToolContext`` and the sandbox lifecycle activities' request/response payloads;
# stringized annotations have tripped its TypeAdapter build elsewhere in this codebase (see
# ``harness/code_mode/batch_models.py``'s identical note).

from typing import Any

from pydantic import BaseModel, Field


class SandboxRef(BaseModel):
    """Serializable pointer to a live (possibly paused) sandboxed-tool sandbox.

    Produced by the ``sandbox_activate``/``sandbox_pause`` activities and persisted as
    ``AgentWorkflowRunner.sandbox_ref`` workflow state, then threaded back into every later
    sandbox-touching activity call (lifecycle or tool) so a worker-local cache miss — a
    restarted worker, a different worker in a multi-worker deployment — can reattach to the
    same live sandbox via ``remote.RemoteSession.resume(...)`` instead of losing or orphaning it.
    """

    backend: str = Field(
        description="The remote-box BackendType member name (e.g. 'SUBPROCESS', 'E2B', "
        "'DAYTONA') — stored by name so refs stay valid across releases."
    )
    sandbox_id: str | None = Field(
        default=None,
        description="Provider-assigned sandbox id; None for backends with no persistent "
        "sandbox identity (e.g. the Subprocess backend).",
    )


class SandboxToolContext(BaseModel):
    """Everything a ``sandboxed=True`` tool's activity needs to reach its agent's live sandbox,
    threaded in as part of :class:`~temporal_agent_harness.harness.agent_workflow.AgentToolContext`.

    ``backend``/``local_project_root`` are carried as a plain JSON-compatible dump — NOT
    remote-box's own typed ``AnyBackendConfig`` — specifically so this dependency-free leaf
    module (and thus ``AgentToolContext``/core ``agent_workflow.py``) never needs the optional
    ``sandbox`` extra installed just to hold this value. The worker-side sandboxed
    ``activity_body`` branch (which already requires remote-box to run the tool at all)
    reconstructs the typed backend config from ``backend`` via
    ``sandbox.activities.backend_from_dict``.

    Threading the full backend config on every sandboxed tool call (not just the lifecycle
    activities) — rather than relying solely on the worker-process-local session cache — is what
    lets a tool activity self-heal via ``RemoteSession.resume(ref, ...)`` even if it lands on a
    worker process that never ran this run's ``sandbox_activate`` (a different worker in a
    multi-worker deployment, or the same worker after a restart).
    """

    ref: SandboxRef | None = None
    backend: dict[str, Any]
    local_project_root: str
