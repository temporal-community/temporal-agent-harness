# ABOUTME: A single sandboxed=True tool + the SandboxConfig that decides its backend. Kept in
# its own plain (non-workflow, non-worker) module: remote-box re-imports this module fresh inside
# the sandbox subprocess for every call, so it stays minimal on purpose (see
# `harness/sandbox/config.py`'s docstring for why SandboxConfig itself needs remote-box, and
# `harness/agent_workflow.py`'s `_validate_sandboxable` for why a sandboxed tool takes exactly
# one pydantic.BaseModel param and returns one).

import subprocess
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel
from remote import Daytona
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox import SandboxConfig

# The ONE place this demo's sandbox backend is chosen — never the tool itself. Runs under a real
# Daytona cloud sandbox. Swap in remote.E2B(...) or remote.Subprocess() to run the exact same
# tool under a different backend, with zero changes to `run_bash` below.
#
# local_project_root is the REPO ROOT, not this directory: `run_bash` is re-imported inside the
# sandbox by its full dotted path (`examples.sandboxed_tool_demo.tools`), which resolves only
# with the repo root (where the `examples` namespace package starts) on the path.
#
# dockerfile_path points at the REPO ROOT, not this directory — see Dockerfile.sandboxed-tool-demo
# there for why: remote-box's Daytona backend resolves every Dockerfile COPY source relative to
# the Dockerfile's OWN directory (a daytona_sdk quirk, confirmed by reading
# Image.from_dockerfile's source — local_project_root plays no part in that resolution), so the
# Dockerfile must itself live at local_project_root for `COPY pyproject.toml uv.lock ./`/
# `COPY temporal_agent_harness/ ...` to find anything. That same Dockerfile also lists every COPY
# source EXPLICITLY rather than `COPY . .` — Daytona's SDK builds its own upload list by parsing
# the Dockerfile's own COPY lines client-side, so `.dockerignore` is never consulted at all; a
# bare `COPY . .` would upload `.venv`, `.git`, and `.env.local` (which holds real API keys).
#
# Needs DAYTONA_API_KEY set (in .env.local) and the snapshot built ahead of time — never at
# runtime (SandboxConfig.require_prebuilt defaults to True) — via:
#   from temporal_agent_harness.harness.sandbox import build_sandbox
#   build_sandbox(SANDBOX)
SANDBOX = SandboxConfig(
    backend=Daytona(
        snapshot_name="sandboxed-tool-demo",
        dockerfile_path="Dockerfile.sandboxed-tool-demo",
    ),
    local_project_root=Path(__file__).parent.parent.parent,
)


class RunBashInput(BaseModel):
    command: str


class RunBashResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


@agent.activity_tool_defn(
    sandboxed=True,
    # Arbitrary bash commands can run long (installs, builds, ...) — a generous timeout, well
    # past the harness's 30s tool-call default.
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(minutes=2)),
)
async def run_bash(arg: RunBashInput) -> RunBashResult:
    """Run an arbitrary bash command inside an isolated sandbox and return its stdout, stderr,
    and exit code.

    Runs INSIDE the sandbox (Daytona/E2B/Subprocess — whichever this agent is configured with),
    never on the worker's own machine. Every call requires human approval before it runs (see
    workflow.py's approval_policy_default) — deliberately NOT `inherently_safe`, since a bash
    command can do anything a shell can, and no approval policy should ever auto-approve it.
    """
    proc = subprocess.run(
        ["bash", "-c", arg.command],
        capture_output=True,
        text=True,
        timeout=100,  # comfortably under the activity's own 2-minute start_to_close_timeout
    )
    return RunBashResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)
