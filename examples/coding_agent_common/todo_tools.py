"""The agent's task-list tools — shared by both coding-agent examples.

``todowrite``/``todoread`` are ordinary ``@agent.tool_defn`` tools (NOT callback, NOT sandboxed):
recording a plan is the agent's own state, not an action on any project, so they run INLINE in the
workflow and read/write workflow-owned state (the ``sink``, an ``Injected`` list the workflow
supplies per call). Marked ``inherently_safe`` so the approval policy auto-approves them.

NB: no ``from __future__ import annotations`` — the parameter/return annotations are read directly
to build the model-facing tool schema and the output-type validator, so they must be concrete types.
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


def as_items(todos: list) -> list[TodoItem]:
    """Coerce the model's raw JSON (a list of dicts) into ``TodoItem``s. The inline-tool path hands
    arguments through as plain dicts, so validate here — this also applies the ``status`` default
    when the model omits it, and rejects malformed items with a clear error."""
    return [t if isinstance(t, TodoItem) else TodoItem.model_validate(t) for t in todos]


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
    items = as_items(todos)
    sink[:] = items  # replace the workflow's durable todo state in place (typed TodoItems)
    done = sum(1 for t in items if t.status == "completed")
    active = next((t.content for t in items if t.status == "in_progress"), None)
    return f"Task list updated ({done}/{len(items)} complete)" + (f"; now: {active}" if active else "")


@agent.tool_defn(inherently_safe=True)
async def todoread(sink: agent.Injected[list]) -> str:
    """Return your current task list — the one you last set with `todowrite`. It's kept as durable
    workflow state, so use this to recall your plan and where you left off (e.g. at the start of a
    follow-up request). Takes no arguments. Each line is `[status] content`."""
    items = as_items(sink)
    if not items:
        return "(no todos yet)"
    return "\n".join(f"[{t.status}] {t.content}" for t in items)
