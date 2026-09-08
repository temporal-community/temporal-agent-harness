"""The agent's plan tool.

A coding agent that works multi-step keeps a visible plan: it lays out the
steps, marks one in progress, and ticks them off as it goes, so a human can
follow along and the agent stays on track. That is what `update_plan` records.

It is an inline harness tool, not a sandbox tool: the plan is the agent's own
state (durable workflow state in the durable mode), not an action on the
project, so it runs in-process and writes to the injected `sink`.

NB: no `from __future__ import annotations`. The annotations are read at runtime
to build the model-facing schema, so they must be concrete types.
"""

from typing import Literal

from pydantic import BaseModel

from temporal_agent_harness.harness import agent

TodoStatus = Literal["pending", "in_progress", "completed"]


class Todo(BaseModel):
    """One step in the plan."""

    step: str
    """Short imperative description, e.g. "Add a test for the parser"."""
    status: TodoStatus = "pending"
    """Keep exactly one step `in_progress` at a time."""


def render_plan(todos: list[Todo]) -> str:
    if not todos:
        return "(no plan yet)"
    return "\n".join(f"[{t.status}] {t.step}" for t in todos)


@agent.tool_defn(inherently_safe=True)
async def update_plan(todos: list[Todo], sink: agent.Injected[list]) -> str:
    """Record or update your step-by-step plan for the current task. Pass the FULL list every
    time; it replaces the previous plan. Each item is `{step, status}` where status is one of
    "pending", "in_progress", "completed". Lay out the steps before you start, mark one step
    `in_progress`, and mark steps `completed` as you finish them, keeping exactly one in progress.
    Skip the plan for a trivial one-step change."""
    items = [t if isinstance(t, Todo) else Todo.model_validate(t) for t in todos]
    sink[:] = items
    done = sum(1 for t in items if t.status == "completed")
    active = next((t.step for t in items if t.status == "in_progress"), None)
    return f"Plan updated ({done}/{len(items)} done)" + (f"; in progress: {active}" if active else "")


PLAN_TOOLS = [update_plan]
