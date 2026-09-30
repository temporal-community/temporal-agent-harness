# ABOUTME: End-to-end test of the builder's `check_code` tool: it fetches the script through the
# `export_code` callback (answered here, as the studio page would) and type-checks it with
# `agent.code_mode_type_check` against the flow contract, without running it — so a clean script
# passes, a bad one comes back with errors by line, and no step agent is ever started.
#
# Run with: uv run --group examples pytest tests/examples/agent_dag -v

from __future__ import annotations

import uuid

import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_protocol import (
    PROVIDE_CALLBACK_RESULT_UPDATE,
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    CallbackResult,
    CallbackResultAck,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from ._check_parent import CheckCodeParentWorkflow
from .test_execute_flow import FLOW, TYPED_FLOW


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"check-code-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CheckCodeParentWorkflow],
        plugins=[AgentHarnessPlugin()],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _check(client: Client, task_queue: str, script: str) -> tuple[str, list[AgentEvent]]:
    """Run one `check_code`, answering its `export_code` callback with `script`."""
    handle = await client.start_workflow(
        CheckCodeParentWorkflow.run,
        AgentConfig(),
        id=f"CheckCodeParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="check", payload={"text": ""}),
        result_type=AgentMessageReply,
    )
    events: list[AgentEvent] = []
    reply = ""
    stream = WorkflowStreamClient.create(client, handle.id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        events.append(envelope)
        event = envelope.event
        if event.type == AgentEventType.CALLBACK_REQUESTED:
            assert event.tool_name == "export_code"
            await handle.execute_update(
                PROVIDE_CALLBACK_RESULT_UPDATE,
                CallbackResult(tool_id=event.tool_id, result=script),
                result_type=CallbackResultAck,
            )
        if event.type == AgentEventType.MESSAGE_HANDLER_END:
            reply = event.output["text"]
        if event.type == AgentEventType.TURN_END:
            break
    return reply, events


async def test_a_clean_flow_passes(client_and_queue):
    client, task_queue = client_and_queue
    reply, events = await _check(client, task_queue, FLOW)

    assert reply.startswith("No errors")
    # Checking runs nothing: no step agent starts.
    assert not [e for e in events if e.event.type == AgentEventType.SUBAGENT_STARTED]


async def test_errors_come_back_with_their_lines(client_and_queue):
    client, task_queue = client_and_queue
    script = FLOW.replace('"search_flights"', '"launch_rockets"').replace(
        "plan[\"output\"]", "plan[\"outptu\"]"
    )
    reply, events = await _check(client, task_queue, script)

    assert reply.startswith("The script has errors")
    assert "launch_rockets" in reply or "tools" in reply
    assert 'Unknown key "outptu"' in reply
    assert "main.py:" in reply
    assert not [e for e in events if e.event.type == AgentEventType.SUBAGENT_STARTED]


async def test_a_flow_that_calls_a_step_type_passes(client_and_queue):
    client, task_queue = client_and_queue
    reply, _ = await _check(client, task_queue, TYPED_FLOW)
    assert reply.startswith("No errors"), reply


async def test_an_empty_editor_is_reported(client_and_queue):
    client, task_queue = client_and_queue
    reply, _ = await _check(client, task_queue, "")
    assert reply == "The editor is empty; write the flow first."
