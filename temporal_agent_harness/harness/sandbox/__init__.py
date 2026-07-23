"""Sandboxed activity tools: run a `@agent.activity_tool_defn(sandboxed=True)` tool's body
inside a remote-box sandbox (Subprocess/E2B/Daytona) instead of directly in the worker process.

Requires the optional `sandbox` extra (`uv sync --extra sandbox`, Python >= 3.12) — importing
this package (directly, or transitively via `SandboxConfig`/`build_sandbox`/`check_sandbox`) is
how an agent author opts into that dependency. Core harness code (`agent_workflow.py`, and thus
plain `from temporal_agent_harness.harness import agent`) never imports it, so agents that don't
use sandboxing never need remote-box installed.

  * `SandboxConfig` — pass to `AgentWorkflowRunner(..., sandbox=SandboxConfig(...))` to choose
    the backend a given agent's sandboxed tools run in.
  * `build_sandbox` / `check_sandbox` — the offline/CI-only entry point that builds (or verifies)
    a `SandboxConfig`'s image ahead of a deploy. Runtime never builds (see
    `SandboxConfig.require_prebuilt`).
  * Worker registration is separate, from `.activities` — mirrors
    `harness/code_mode/activities.py`'s `CODE_MODE_ACTIVITIES` split:

        from temporal_agent_harness.harness.sandbox.activities import SANDBOX_ACTIVITIES
        Worker(..., activities=[*SANDBOX_ACTIVITIES, agent.tool_activity(my_sandboxed_tool), ...])
"""

from temporal_agent_harness.harness.sandbox.build import build_sandbox, check_sandbox
from temporal_agent_harness.harness.sandbox.config import SandboxConfig

__all__ = ["SandboxConfig", "build_sandbox", "check_sandbox"]
