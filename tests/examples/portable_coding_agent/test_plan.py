"""Tests for the plan tool's pure pieces (examples.portable_coding_agent.plan)."""

import pytest

from examples.portable_coding_agent.plan import Todo, render_plan


def test_render_empty_plan():
    assert render_plan([]) == "(no plan yet)"


def test_render_plan_lines():
    todos = [
        Todo(step="read config.py", status="completed"),
        Todo(step="add the flag", status="in_progress"),
        Todo(step="write a test"),
    ]
    assert render_plan(todos) == (
        "[completed] read config.py\n[in_progress] add the flag\n[pending] write a test"
    )


def test_todo_defaults_to_pending():
    assert Todo(step="x").status == "pending"


def test_todo_rejects_unknown_status():
    with pytest.raises(ValueError):
        Todo(step="x", status="blocked")
