"""Sandboxed activity tools: run a `@agent.activity_tool_defn(sandboxed=True)` tool's body
inside a microsandbox microVM (or in-process when ``backend="local"``) instead of directly in
the worker process.

Requires the optional `sandbox` extra (`uv sync --extra sandbox`) — importing this package
is how an agent author opts into that dependency. Core harness code never imports it.

  * `SandboxConfig` — pass to `AgentWorkflowRunner(..., sandbox=SandboxConfig(...))`.
  * `MicrosandboxBackend` — harness-owned microsandbox config (snapshot, cpus, secrets, …).
  * `build_sandbox` / `check_sandbox` — offline/CI snapshot verification.
  * `BackendProvider` — async producer for configs needing runtime I/O (tokens, secrets).
  * Worker registration from `.activities`::

        from temporal_agent_harness.harness.sandbox.activities import SANDBOX_ACTIVITIES
        Worker(..., activities=[*SANDBOX_ACTIVITIES, agent.tool_activity(my_sandboxed_tool)])
"""

from temporal_agent_harness.harness.sandbox.build import TargetResult, build_sandbox, check_sandbox
from temporal_agent_harness.harness.sandbox.config import (
    BackendProvider,
    MicrosandboxBackend,
    SandboxConfig,
)

__all__ = [
    "BackendProvider",
    "MicrosandboxBackend",
    "SandboxConfig",
    "TargetResult",
    "build_sandbox",
    "check_sandbox",
]
