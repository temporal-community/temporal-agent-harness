# ABOUTME: The sandboxed coding agent's six tools + the SandboxConfig picking their backend. Each is
# a @agent.activity_tool_defn(sandboxed=True) tool whose body runs inside a microsandbox microVM
# (or in-process when backend=local), against a project at PROJECT_ROOT. The actual work is the
# SHARED examples.coding_agent_common.tool_impls — the same code the callback coding agent runs on
# the user's laptop; here it runs in the sandbox instead.

import os
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox import MicrosandboxBackend, SandboxConfig

from examples.coding_agent_common import tool_impls

_EXAMPLES = Path(__file__).parent.parent.parent
_LOCAL_WORKSPACE = Path(__file__).parent / "workspace"
_LOCAL_WORKSPACE.mkdir(exist_ok=True)

# Base domain the live-preview proxy serves sandbox subdomains under (e.g. "preview.example.com").
PREVIEW_BASE_DOMAIN = os.environ.get("PREVIEW_BASE_DOMAIN", "").strip().lower()

# In microVM remote exec, tools run at the guest project root; locally, use a writable workspace.
if os.environ.get("REMOTE_EXECUTION_MODE") == "1":
    PROJECT_ROOT = Path("/home/user/project")
else:
    PROJECT_ROOT = Path(os.environ.get("SANDBOX_PROJECT_ROOT", _LOCAL_WORKSPACE))

SANDBOX_ID_ENV_VAR = "MICROSANDBOX_NAME"

# Default ``local`` for dev/demo without msb; set SANDBOX_BACKEND=microsandbox for microVM runs.
_USE_LOCAL = os.environ.get("SANDBOX_BACKEND", "local").strip().lower() == "local"

SANDBOX_BACKEND = MicrosandboxBackend(
    snapshot_name="sandboxed-coding-agent",
    dockerfile_path="Dockerfile.sandbox-coding-agent",
    cpus=2,
    memory=2048,
)

SANDBOX = SandboxConfig(
    backend="local" if _USE_LOCAL else "microsandbox-openai-egress",
    local_project_root=_EXAMPLES,
    require_prebuilt=not _USE_LOCAL,
)

_RETRIES = RetryPolicy(maximum_attempts=3)

_BASH_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=3),
    retry_policy=_RETRIES,
)

_FAST_ACTIVITY = ActivityConfig(
    start_to_close_timeout=timedelta(seconds=30),
    retry_policy=_RETRIES,
)


class BashInput(BaseModel):
    command: str


class BashResult(BaseModel):
    output: str
    exit_code: int


class ReadInput(BaseModel):
    file_path: str


class ReadResult(BaseModel):
    content: str


class WriteInput(BaseModel):
    file_path: str
    content: str


class WriteResult(BaseModel):
    message: str


class EditInput(BaseModel):
    file_path: str
    old_string: str
    new_string: str


class EditResult(BaseModel):
    message: str
    diff: str


class GrepInput(BaseModel):
    pattern: str


class GrepResult(BaseModel):
    matches: str
    count: int


class GlobInput(BaseModel):
    pattern: str


class GlobResult(BaseModel):
    paths: str
    count: int


@agent.activity_tool_defn(sandboxed=True, activity_config=_BASH_ACTIVITY)
async def bash(arg: BashInput) -> BashResult:
    """Run a shell command in the project directory and return its combined stdout+stderr and exit
    code. Use it to scaffold the project, install deps, build, run tests, use `git`, or start the
    server. Runs inside the sandbox, in the project root. To make a site previewable, write the
    launch command to `start.sh` in the project root (foreground, bound to 0.0.0.0) — a supervisor
    runs it.
    Prefer `write`/`edit` for file changes so the result shows a clean diff. Gated on approval."""
    output, exit_code = await tool_impls.bash_exec(PROJECT_ROOT, arg.command)
    return BashResult(output=output, exit_code=exit_code)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True, activity_config=_FAST_ACTIVITY)
async def read(arg: ReadInput) -> ReadResult:
    """Read a UTF-8 text file from the project and return its full contents. `file_path` is relative
    to the project root, e.g. "src/main.py". Always read a file before editing it, so your `edit`
    matches the exact current text."""
    return ReadResult(content=tool_impls.read_file(PROJECT_ROOT, arg.file_path))


@agent.activity_tool_defn(sandboxed=True, activity_config=_FAST_ACTIVITY)
async def write(arg: WriteInput) -> WriteResult:
    """Create a new file, or OVERWRITE an existing one, with `content` (UTF-8), creating parent
    directories as needed. Replaces the WHOLE file — use `edit` for a surgical change to a large
    file. `file_path` is relative to the project root. Returns a short confirmation."""
    return WriteResult(message=tool_impls.write_file(PROJECT_ROOT, arg.file_path, arg.content))


@agent.activity_tool_defn(sandboxed=True, activity_config=_FAST_ACTIVITY)
async def edit(arg: EditInput) -> EditResult:
    """Replace an exact substring in a file. `old_string` must occur EXACTLY ONCE (include enough
    surrounding context to make it unique) and is replaced with `new_string`. `read` the file first
    so the match is exact. Returns a confirmation plus a unified diff. `file_path` is relative to the
    project root."""
    message, diff = tool_impls.edit_file(PROJECT_ROOT, arg.file_path, arg.old_string, arg.new_string)
    return EditResult(message=message, diff=diff)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True, activity_config=_FAST_ACTIVITY)
async def grep(arg: GrepInput) -> GrepResult:
    """Search every text file in the project for a Python regular expression, returning matching
    lines as "path:lineno: line". Use it to locate a symbol, string, or definition before reading or
    editing. Results are capped."""
    matches, count = tool_impls.grep_files(PROJECT_ROOT, arg.pattern)
    return GrepResult(matches=matches, count=count)


@agent.activity_tool_defn(sandboxed=True, inherently_safe=True, activity_config=_FAST_ACTIVITY)
async def glob(arg: GlobInput) -> GlobResult:
    """List project files whose path matches a glob `pattern` (e.g. "**/*.py", "src/**/*.ts"), one
    per line, relative to the project root. Use it to discover files by name/extension before reading
    them. Results are capped."""
    paths, count = tool_impls.glob_files(PROJECT_ROOT, arg.pattern)
    return GlobResult(paths=paths, count=count)


SANDBOXED_CODING_TOOLS = [bash, read, write, edit, grep, glob]
