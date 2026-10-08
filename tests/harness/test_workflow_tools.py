"""Shared tools preserve typed dispatch, recipient boundaries, and SDK schemas."""

import inspect
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness import agent_workflow as aw


@workflow.defn
class TypedChild:
    @workflow.run
    async def run(self, question: str, limit: int = 2) -> list[str]:
        """Research a question."""
        return [question] * limit


@pytest.fixture
def tool_context(monkeypatch):
    events = []
    runner = SimpleNamespace(
        current_stream_context=SimpleNamespace(turn_id="turn", turn_number=1),
        _auto_approves=lambda *args, **kwargs: True,
        _pub=lambda *args: events.append(args[-1]),
    )
    runner_token = aw._CURRENT_RUNNER.set(runner)
    tool_token = aw._CURRENT_TOOL_ID.set("call")
    monkeypatch.setattr(workflow, "in_workflow", lambda: True)
    try:
        yield events
    finally:
        aw._CURRENT_TOOL_ID.reset(tool_token)
        aw._CURRENT_RUNNER.reset(runner_token)


async def test_child_typed_result_defaults_and_lifecycle(monkeypatch, tool_context):
    execute = AsyncMock(return_value=["answer"])
    monkeypatch.setattr(workflow, "execute_child_workflow", execute)
    tool = agent.child_workflow_as_tool(
        TypedChild.run,
        tool_name="research",
        task_queue="specialists",
        run_timeout=timedelta(minutes=2),
    )
    assert await tool(question="why?") == ["answer"]
    args, options = execute.call_args
    assert args == ("TypedChild",)
    assert options["args"] == ["why?", 2]
    assert options["id"] is None
    assert options["result_type"] == list[str]
    assert options["task_queue"] == "specialists"
    assert options["run_timeout"] == timedelta(minutes=2)
    assert options["parent_close_policy"] == workflow.ParentClosePolicy.TERMINATE
    assert [type(e).__name__ for e in tool_context] == [
        "ToolStartEvent",
        "ToolEndEvent",
    ]
    assert list(inspect.signature(tool).parameters) == ["question", "limit"]


async def test_approval_precedes_child_dispatch(monkeypatch, tool_context):
    execute = AsyncMock()
    monkeypatch.setattr(workflow, "execute_child_workflow", execute)

    async def denied(*args, **kwargs):
        raise RuntimeError("approval denied")

    monkeypatch.setattr(aw, "_apply_approval_policy", denied)
    with pytest.raises(RuntimeError, match="approval denied"):
        await agent.child_workflow_as_tool(TypedChild.run)("why?")
    execute.assert_not_called()
    assert tool_context == []


def test_child_rejects_undecorated_function():
    async def plain(question: str) -> str:
        return question

    with pytest.raises(TypeError, match="decorated Workflow"):
        agent.child_workflow_as_tool(plain)


async def test_message_alias_snapshot_and_parent(monkeypatch, tool_context):
    signal = AsyncMock()
    targets = []

    def handle(target):
        targets.append(target)
        return SimpleNamespace(signal=signal)

    monkeypatch.setattr(workflow, "get_external_workflow_handle", handle)
    monkeypatch.setattr(
        workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="child",
            run_id="run",
            parent=SimpleNamespace(workflow_id="parent-id"),
        ),
    )
    monkeypatch.setattr(workflow, "uuid4", lambda: UUID(int=1))
    recipients = {"reviewer": "reviewer-id"}
    tool = agent.send_message_tool(recipients, include_parent=True)
    recipients["reviewer"] = "changed-id"
    assert await tool("reviewer", "review this") == "Message sent to reviewer."
    await tool("parent", "progress")
    assert targets == ["reviewer-id", "parent-id"]
    name, envelope = signal.call_args.args
    assert name == "temporal_agent_harness.receive_message"
    assert envelope == agent.WorkflowMessage(
        str(UUID(int=1)), "child", "run", "progress"
    )
    with pytest.raises(ApplicationError, match="Unknown message recipient"):
        await tool("arbitrary-workflow-id", "hello")
    assert signal.await_count == 2
    assert type(tool_context[-1]).__name__ == "ToolErrorEvent"


async def test_parent_requires_child(monkeypatch, tool_context):
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(parent=None))
    with pytest.raises(ApplicationError, match="requires a Child Workflow"):
        await agent.send_message_tool({}, include_parent=True)("parent", "hello")


async def test_inbox_fifo_drain_and_timeout(monkeypatch):
    handlers = {}
    monkeypatch.setattr(
        workflow, "set_signal_handler", lambda name, fn: handlers.setdefault(name, fn)
    )
    wait = AsyncMock()
    monkeypatch.setattr(workflow, "wait_condition", wait)
    inbox = agent.AgentMessageInbox(signal_name="custom")
    first = agent.WorkflowMessage("1", "sender", "run", "one")
    second = agent.WorkflowMessage("2", "sender", "run", "two")
    handlers["custom"](first)
    handlers["custom"](second)
    assert inbox.messages == (first, second)
    assert await inbox.receive(timeout=5) == first
    assert wait.call_args.kwargs["timeout"] == 5
    assert inbox.drain() == [second]
    assert inbox.messages == ()


def test_openai_and_pydantic_adapters_accept_shared_tools():
    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        as_openai_agent_tool,
    )
    from temporal_agent_harness.ai_sdks.pydantic_ai_harness import as_pydantic_ai_tool

    for tool in [
        agent.child_workflow_as_tool(TypedChild.run),
        agent.send_message_tool({"peer": "id"}),
    ]:
        openai = as_openai_agent_tool(SimpleNamespace(), tool)
        pydantic = as_pydantic_ai_tool(tool)
        assert openai.name == pydantic.name == tool.__name__
        expected = set(inspect.signature(tool).parameters)
        assert set(openai.params_json_schema["properties"]) == expected
        assert set(pydantic.function_schema.json_schema["properties"]) == expected


def test_gemini_schema_accepts_shared_tools():
    from temporal_agent_harness.ai_sdks.google_genai_plugin import function_param

    for tool in [
        agent.child_workflow_as_tool(TypedChild.run),
        agent.send_message_tool({"peer": "id"}),
    ]:
        declaration = function_param(tool)
        assert declaration["name"] == tool.__name__
        assert set(declaration["parameters"]["properties"]) == set(
            inspect.signature(tool).parameters
        )
