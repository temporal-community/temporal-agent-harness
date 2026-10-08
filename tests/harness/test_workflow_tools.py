"""Message routing uses the existing subagent registry and tool policies."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness import agent_workflow as aw


@pytest.fixture
def tool_context(monkeypatch):
    events = []
    status = aw._WorkflowStatus(
        agent_id="parent",
        is_message_queuing_enabled=False,
        approval_policy=agent.ToolApprovalPolicy.dangerously_skip_all(),
    )
    runner = SimpleNamespace(
        _status=status,
        current_stream_context=SimpleNamespace(turn_id="turn", turn_number=1),
        _auto_approves=lambda *args, **kwargs: True,
        _pub=lambda *args: events.append(args[-1]),
    )
    runner.publish = lambda event: events.append(event)
    monkeypatch.setattr(workflow, "patched", lambda _: True)
    runner.subagent_workflow_id = (
        lambda handle: aw.AgentWorkflowRunner.subagent_workflow_id(runner, handle)
    )
    runner_token = aw._CURRENT_RUNNER.set(runner)
    tool_token = aw._CURRENT_TOOL_ID.set("call")
    monkeypatch.setattr(workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(
        workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="parent-id", run_id="parent-run", parent=None
        ),
    )
    monkeypatch.setattr(workflow, "uuid4", lambda: UUID(int=1))
    try:
        yield runner, events
    finally:
        aw._CURRENT_TOOL_ID.reset(tool_token)
        aw._CURRENT_RUNNER.reset(runner_token)


async def test_parent_routes_to_owned_child_handle(monkeypatch, tool_context):
    runner, events = tool_context
    runner._status.register_subagent("child-handle", "child-workflow", "researcher")
    signal = AsyncMock()
    targets = []

    def external(target):
        targets.append(target)
        return SimpleNamespace(signal=signal)

    monkeypatch.setattr(workflow, "get_external_workflow_handle", external)
    assert (
        await agent.send_message_tool()("child-handle", "instructions")
        == "Message sent to child-handle."
    )
    assert targets == ["child-workflow"]
    name, message = signal.call_args.args
    assert name == "temporal_agent_harness.receive_message"
    assert message == agent.WorkflowMessage(
        str(UUID(int=1)), "parent-id", "parent-run", "instructions"
    )
    assert [type(e).__name__ for e in events] == [
        "ToolStartEvent",
        "AgentMessageSent",
        "ToolEndEvent",
    ]


async def test_unknown_workflow_ids_and_stopped_handles_rejected(
    monkeypatch, tool_context
):
    runner, events = tool_context
    runner._status.register_subagent("child-handle", "child-workflow", "researcher")
    external = AsyncMock()
    monkeypatch.setattr(workflow, "get_external_workflow_handle", external)
    tool = agent.send_message_tool()
    for recipient in ["arbitrary-id", "child-workflow"]:
        with pytest.raises(ApplicationError, match="Unknown subagent"):
            await tool(recipient, "hello")
    runner._status.remove_subagent("child-handle")
    with pytest.raises(ApplicationError, match="Unknown subagent"):
        await tool("child-handle", "late message")
    external.assert_not_called()
    assert type(events[-1]).__name__ == "ToolErrorEvent"


async def test_child_routes_to_parent(monkeypatch, tool_context):
    monkeypatch.setattr(
        workflow,
        "info",
        lambda: SimpleNamespace(
            workflow_id="child",
            run_id="child-run",
            parent=SimpleNamespace(workflow_id="parent-id"),
        ),
    )
    signal = AsyncMock()
    targets = []

    def handle(target):
        targets.append(target)
        return SimpleNamespace(signal=signal)

    monkeypatch.setattr(workflow, "get_external_workflow_handle", handle)
    await agent.send_message_tool()("parent", "progress")
    assert targets == ["parent-id"]
    assert signal.call_args.args[1].sender_workflow_id == "child"


async def test_parent_requires_parent_workflow(tool_context):
    with pytest.raises(ApplicationError, match="no parent workflow"):
        await agent.send_message_tool()("parent", "hello")


async def test_approval_precedes_message_delivery(monkeypatch, tool_context):
    external = AsyncMock()
    monkeypatch.setattr(workflow, "get_external_workflow_handle", external)

    async def denied(*args, **kwargs):
        raise RuntimeError("approval denied")

    monkeypatch.setattr(aw, "_apply_approval_policy", denied)
    with pytest.raises(RuntimeError, match="approval denied"):
        await agent.send_message_tool()("child-handle", "hello")
    external.assert_not_called()
    assert tool_context[1] == []


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


def test_adapters_accept_message_tool():
    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        as_openai_agent_tool,
    )
    from temporal_agent_harness.ai_sdks.pydantic_ai_harness import as_pydantic_ai_tool
    from temporal_agent_harness.ai_sdks.google_genai_plugin import function_param

    tool = agent.send_message_tool()
    openai = as_openai_agent_tool(SimpleNamespace(), tool)
    pydantic = as_pydantic_ai_tool(tool)
    gemini = function_param(tool)
    expected = set(inspect.signature(tool).parameters)
    assert expected == {"recipient", "message"}
    assert set(openai.params_json_schema["properties"]) == expected
    assert set(pydantic.function_schema.json_schema["properties"]) == expected
    assert set(gemini["parameters"]["properties"]) == expected


def test_workflow_tool_wrapper_removed():
    assert not hasattr(agent, "child_workflow_as_tool")


async def test_openai_adapter_dispatches_existing_subagent_operation(tool_context):
    from temporal_agent_harness.ai_sdks.openai_agents_harness import (
        as_openai_agent_tool,
    )
    from temporal_agent_harness.harness.agent_protocol import TextMessage, TextReply

    class Child:
        @agent.accepts
        async def ask(self, message: TextMessage) -> TextReply:
            """Answer a question."""
            ...

    runner, _ = tool_context
    runner.run_subagent_turn = AsyncMock(return_value={"text": "answer"})

    async def run_tool(call_id, tool, *args, injections=None, **kwargs):
        return await tool(*args, **kwargs)

    runner.run_tool = run_tool
    _, ask, _ = agent.subagent_toolset(Child, key="child", task_queue="children")
    tool = as_openai_agent_tool(runner, ask)
    result = await tool.on_invoke_tool(
        SimpleNamespace(tool_call_id="ask"),
        '{"subagent":"child-handle","message":{"text":"why?"}}',
    )
    assert "answer" in result and "failed" not in result
    runner.run_subagent_turn.assert_awaited_once_with(
        "child-handle", "ask", {"text": "why?"}
    )
    # Gemini and Pydantic AI dispatch keyword arguments against the same schema.
    assert (
        await ask(subagent="child-handle", message={"text": "again"})
    ).text == "answer"
