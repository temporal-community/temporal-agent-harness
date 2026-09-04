"""Static check that the sandbox coding agent wires harness primitives."""

import uuid
from typing import cast
from unittest.mock import MagicMock

from temporalio import activity
from temporalio.client import Client

import temporal_agent_harness.harness.agent_workflow as aw
from temporal_agent_harness.harness import slash_commands
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporal_agent_harness.harness.agent_workflow import agent_resumption_hooks
from temporal_agent_harness.harness.subagent_activities import RUN_SUBAGENT_TURN_ACTIVITY
from temporal_agent_harness.harness.subagent_toolset import subagent_toolset

from examples.coding_agent_common.todo_tools import TodoItem, todoread, todowrite
from examples.sandbox_tools.coding_agent.tools import SANDBOXED_CODING_TOOLS
from examples.sandbox_tools.coding_agent.worker import worker_activities
from examples.sandbox_tools.coding_agent.workflow import (
    DEFAULT_MODEL,
    SUPPORTED_MODELS,
    SandboxedCodingAgentWorkflow,
    _subagent_policy,
    model_slash_command,
)


def test_task_toolset_generates_start_ask_stop():
    tools = subagent_toolset(
        SandboxedCodingAgentWorkflow, key="task", task_queue="sandboxed-coding-agent"
    )
    assert {t.__name__ for t in tools} == {"start_task", "task_ask", "stop_task"}


def test_the_worker_hosts_the_activity_that_drives_a_subagent():
    names = {
        activity._Definition.must_from_callable(fn).name
        for fn in worker_activities(cast(Client, object()))
    }
    assert RUN_SUBAGENT_TURN_ACTIVITY in names


def test_subagent_policy_gates_delegation_by_depth():
    assert _subagent_policy(None) == (True, True)
    assert _subagent_policy("abcdef") == (True, True)
    assert _subagent_policy("abcdef-123456") == (False, False)
    assert _subagent_policy("abcdef-123456-789abc") == (False, False)


def test_coding_and_plan_tool_names():
    assert {t.__name__ for t in SANDBOXED_CODING_TOOLS} == {
        "bash",
        "read",
        "write",
        "edit",
        "grep",
        "glob",
    }
    assert {t.__name__ for t in (todowrite, todoread)} == {"todowrite", "todoread"}


def test_operator_commands_are_harness_defaults_plus_model():
    names = [
        definition.command.name
        for definition in (
            *slash_commands.default_commands(),
            model_slash_command(lambda _: None),
        )
    ]
    assert names == ["approvals", "allow-tools", "status", "stop", "model"]


def test_resumption_hooks_exist():
    assert agent_resumption_hooks(SandboxedCodingAgentWorkflow) is not None


def test_runner_owns_stream_and_carries_conversation_across_snapshot(monkeypatch):
    """Rollover needs a runner-built stream plus snapshot/restore of conversation, model, plan."""
    for handler in ("set_update_handler", "set_query_handler", "set_signal_handler"):
        monkeypatch.setattr(aw.workflow, handler, lambda *a, **k: None)
    monkeypatch.setattr(aw.workflow, "time", lambda: 0.0)
    monkeypatch.setattr(aw.workflow, "uuid4", lambda: uuid.uuid4())
    monkeypatch.setattr(aw, "WorkflowStream", MagicMock())

    source = SandboxedCodingAgentWorkflow(AgentConfig())
    assert source._runner._owns_stream is True
    assert [c.name for c in source._runner._handle_operator_interface()] == [
        "approvals",
        "allow-tools",
        "status",
        "stop",
        "model",
    ]

    chosen = next(m for m in SUPPORTED_MODELS if m != DEFAULT_MODEL)
    source._conversation = [{"role": "user", "content": "hi"}]
    source._set_model(chosen)
    source._todos = [TodoItem(content="scaffold the app", status="in_progress")]

    target = SandboxedCodingAgentWorkflow(AgentConfig())
    target.restore(source.snapshot())
    assert target._conversation == source._conversation
    assert target._model == chosen
    assert [t.content for t in target._todos] == ["scaffold the app"]
    assert all(isinstance(t, TodoItem) for t in target._todos)
