from __future__ import annotations

import json
import uuid

import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.openai_agents import OpenAIPayloadConverter
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
    ResponseBuilders,
    TestModel,
)
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    ToolApprovalPolicy,
)

from examples.monty import activities
from examples.monty.conversational_workflow import (
    OPENAI_MODEL_OPERATOR_COMMAND,
    OPENAI_SUPPORTED_MODELS,
    MontyChatOpenAIAgentWorkflow,
)
from examples.monty.monty_activities import monty_resume_batch, monty_start_batch


def _const_script(value: int) -> str:
    return (
        "import asyncio\n"
        "async def main():\n"
        f"    return {value}\n"
        "asyncio.run(main())"
    )


@pytest_asyncio.fixture
async def client_and_queue():
    model = TestModel.returning_responses(
        [
            ResponseBuilders.tool_call(
                json.dumps({"script": _const_script(42)}),
                "run_monty_script",
            ),
            ResponseBuilders.output_message("The script returned 42."),
        ]
    )
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=DataConverter(payload_converter_class=OpenAIPayloadConverter)
    )
    task_queue = f"monty-openai-agent-test-{uuid.uuid4()}"
    async with AgentEnvironment(model=model) as agent_env:
        client = agent_env.applied_on_client(env.client)
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[MontyChatOpenAIAgentWorkflow],
            activities=[
                *activities.ALL_ACTIVITIES,
                monty_start_batch,
                monty_resume_batch,
            ],
        ):
            try:
                yield client, task_queue
            finally:
                await env.shutdown()


async def test_openai_chat_agent_runs_monty_tool(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        MontyChatOpenAIAgentWorkflow.run,
        AgentConfig(approval_policy=ToolApprovalPolicy.dangerously_skip_all()),
        id=f"MontyChatOpenAIAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type="ask",
            payload={"text": "Run a script returning 42."},
            expected_turn=1,
        ),
        result_type=AgentMessageReply,
    )

    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
    ):
        events.append(item.data)
        if item.data.event.type == AgentEventType.TURN_END:
            break

    event_types = [event.event.type for event in events]
    assert event_types.count(AgentEventType.MODEL_INTERACTION_STARTED) == 2
    assert event_types.count(AgentEventType.MODEL_INTERACTION_ENDED) == 2
    assert AgentEventType.TOOL_START in event_types
    assert AgentEventType.TOOL_END in event_types

    reply = next(
        event.event
        for event in events
        if event.event.type == AgentEventType.REPLY
    )
    assert reply.output["text"] == "The script returned 42."

    tool_end = next(
        event.event
        for event in events
        if event.event.type == AgentEventType.TOOL_END
    )
    assert "result: 42" in tool_end.tool_output


async def test_openai_chat_agent_model_command_choices():
    assert OPENAI_MODEL_OPERATOR_COMMAND.argument is not None
    assert tuple(OPENAI_MODEL_OPERATOR_COMMAND.argument.choices) == OPENAI_SUPPORTED_MODELS
    assert OPENAI_SUPPORTED_MODELS == ("gpt-5.4-mini", "gpt-5.4")
