"""Tools the coding agent uses to work on a project.

Most are ``@agent.callback_tool_defn`` — tools with NO worker-side body. The agent (picture it
running in a cloud worker with no access to your disk) pauses in-workflow and publishes a
``callback_requested`` event; the OpenCode shim attached on the user's laptop executes the
operation against the local project directory and returns the result via the
``provide_callback_result`` update. The agent never touches a filesystem — it just calls these
like any tool and reasons over the results.

The exception is ``todowrite``: recording a plan is the agent's own state, not an action on the
user's machine, so it's an ordinary ``@agent.tool_defn`` that runs INLINE in the workflow and
writes to workflow-owned state (no client round-trip).

The tool NAMES and parameter names are deliberately snake_case here (idiomatic Python); the shim
maps them to OpenCode's canonical camelCase (``filePath``/``oldString``/``newString``) when it
renders the tool card, so the TUI's rich rendering (e.g. the ``edit`` diff view) lights up.

NB: no ``from __future__ import annotations`` here. The parameter + return annotations are read
directly to build the model-facing tool schemas (via the Gemini plugin's ``function_param``) and
the output-type validator, so they must be concrete types, not stringized.
"""

from typing import Literal

from pydantic import BaseModel

from temporal_agent_harness.harness import agent


TodoStatus = Literal["pending", "in_progress", "completed", "cancelled"]


class TodoItem(BaseModel):
    """One task in the agent's plan (mirrors OpenCode's todo shape)."""

    content: str
    """Short imperative description of the task, e.g. "Add a test for the parser"."""
    status: TodoStatus = "pending"
    """Task state. Keep exactly one task `in_progress` at a time."""


def _as_items(todos: list) -> list[TodoItem]:
    """Coerce the model's raw JSON (a list of dicts) into ``TodoItem``s. The inline-tool path hands
    arguments through as plain dicts, so validate here — this also applies the ``status`` default
    when the model omits it, and rejects malformed items with a clear error."""
    return [t if isinstance(t, TodoItem) else TodoItem.model_validate(t) for t in todos]


# NOT a callback tool: recording a plan has no effect on the user's machine — it's the agent's own
# state — so it runs INLINE in the workflow and writes to workflow-owned state (the `sink`, an
# Injected list the workflow supplies per call). Marked inherently_safe so the approval policy can
# auto-approve it; there's nothing to guard.
@agent.tool_defn(inherently_safe=True)
async def todowrite(todos: list[TodoItem], sink: agent.Injected[list]) -> str:
    """Record or update your task list for the current piece of work — pass the FULL list every
    time (it replaces the previous one).

    Each item in `todos` is an object:
        - `content`: string — a short imperative task description, e.g. "Add a test for the parser".
        - `status`: one of "pending", "in_progress", "completed", "cancelled" (defaults to
          "pending").

    Example: `todos=[{"content": "Read config.py", "status": "in_progress"},
    {"content": "Add the flag", "status": "pending"}]`.

    Use it to plan multi-step work and to keep the user posted on progress: mark a task
    `in_progress` before you start it and `completed` as soon as it's done, keeping exactly one
    task in progress. For trivial single-step requests, skip this."""
    items = _as_items(todos)
    sink[:] = items  # replace the workflow's durable todo state in place (typed TodoItems)
    done = sum(1 for t in items if t.status == "completed")
    active = next((t.content for t in items if t.status == "in_progress"), None)
    return f"Task list updated ({done}/{len(items)} complete)" + (f"; now: {active}" if active else "")


@agent.tool_defn(inherently_safe=True)
async def todoread(sink: agent.Injected[list]) -> str:
    """Return your current task list — the one you last set with `todowrite`. It's kept as durable
    workflow state, so use this to recall your plan and where you left off (e.g. at the start of a
    follow-up request). Takes no arguments. Each line is `[status] content`."""
    items = _as_items(sink)
    if not items:
        return "(no todos yet)"
    return "\n".join(f"[{t.status}] {t.content}" for t in items)


@agent.callback_tool_defn()
async def bash(command: str) -> str:
    """Run a shell command in the project directory and return its combined stdout+stderr. Use it
    to build, run tests, inspect the tree (`ls`, `cat`), use `git`, or anything else a shell can
    do. The command runs on the user's machine, in their project root. Prefer the dedicated
    `read`/`write`/`edit` tools for file edits so the user sees a clean diff. Every call is gated
    on the user's approval, so explain risky commands in your reply."""
    ...


# read/grep/glob are inherently_safe: they only READ the project, so `allow_inherently_safe()`
# auto-approves them (no permission prompt) and they run concurrently during the "orient" phase.
# The mutating tools (bash/write/edit) stay gated.
@agent.callback_tool_defn(inherently_safe=True)
async def read(file_path: str) -> str:
    """Read a UTF-8 text file from the project and return its full contents. `file_path` is
    relative to the project root, e.g. "src/main.py". Always read a file before editing it, so
    your `edit` matches the exact current text."""
    ...


@agent.callback_tool_defn()
async def write(file_path: str, content: str) -> str:
    """Create a new file, or OVERWRITE an existing one, with `content` (UTF-8), creating parent
    directories as needed. This replaces the WHOLE file — use `edit` for a surgical change to a
    large file. `file_path` is relative to the project root. Returns a short confirmation."""
    ...


@agent.callback_tool_defn()
async def edit(file_path: str, old_string: str, new_string: str) -> str:
    """Replace an exact substring in a file. `old_string` must occur EXACTLY ONCE in the file
    (include enough surrounding context to make it unique) and is replaced with `new_string`.
    `read` the file first so the match is exact. Returns a short confirmation. `file_path` is
    relative to the project root."""
    ...


@agent.callback_tool_defn(inherently_safe=True)
async def grep(pattern: str) -> str:
    """Search every text file in the project for a Python regular expression, returning matching
    lines as "path:lineno: line". Use it to locate a symbol, string, or definition before reading
    or editing. Results are capped."""
    ...


@agent.callback_tool_defn(inherently_safe=True)
async def glob(pattern: str) -> str:
    """List project files whose path matches a glob `pattern` (e.g. "**/*.py", "src/**/*.ts"), one
    per line, relative to the project root. Use it to discover files by name/extension before
    reading them. Results are capped."""
    ...


# The full toolset, in a stable order — handed to the model as its tool menu and used to build the
# name -> tool dispatch map in the workflow. All are callback tools EXCEPT `todowrite`/`todoread`,
# which run inline in the workflow (they edit/read agent state, not the user's disk).
CODING_TOOLS = [bash, read, write, edit, grep, glob, todowrite, todoread]
