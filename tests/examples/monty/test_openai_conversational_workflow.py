from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from agents import Model, ModelResponse
from openai.types.responses import ResponseFunctionToolCall
import pytest_asyncio
from temporalio import activity
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
    RUN_SUBAGENT_TURN_ACTIVITY,
    RunSubagentTurnInput,
    SubagentMessageSent,
    SubagentTurnResult,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

from examples.monty import activities
from examples.monty.conversational_subagent_workflow import (
    MontyChatOpenAISubagentWorkflow,
)
from examples.monty.conversational_workflow import (
    OPENAI_MODEL_OPERATOR_COMMAND,
    OPENAI_SUPPORTED_MODELS,
    MontyChatOpenAIAgentWorkflow,
)
from examples.monty.monty_activities import monty_resume_batch, monty_start_batch
from examples.monty.workflow import MontyDynamicAgentWorkflow


def _const_script(value: int) -> str:
    return (
        "import asyncio\n"
        "async def main():\n"
        f"    return {value}\n"
        "asyncio.run(main())"
    )


def _tool_call_response(
    arguments: dict[str, Any], name: str, *, call_id: str
) -> ModelResponse:
    return ResponseBuilders.model_response(
        ResponseFunctionToolCall(
            arguments=json.dumps(arguments),
            call_id=call_id,
            name=name,
            type="function_call",
            id=call_id,
            status="completed",
        )
    )


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _function_output(input_items: str | list[Any], call_id: str) -> str:
    if not isinstance(input_items, list):
        raise AssertionError(
            f"expected model input list, got {type(input_items).__name__}"
        )
    for item in reversed(input_items):
        if _item_value(item, "call_id") != call_id:
            continue
        output = _item_value(item, "output")
        if output is None:
            output = _item_value(item, "content")
        if output is None:
            output = _item_value(item, "result")
        if output is not None:
            return str(output)
    raise AssertionError(
        f"no function output found for call_id={call_id!r}: {input_items!r}"
    )


class _OpenAISubagentModel(Model):
    """Mock model that starts a subagent, then sends a script to the actual handle."""

    def __init__(self) -> None:
        self.inputs: list[str | list[Any]] = []
        self.system_instructions: list[str] = []
        self.start_call_id = "call_start_monty"
        self.run_call_id = "call_monty_run_script"
        self.subagent_handle: str | None = None
        self.run_result_output: str | None = None

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        *_args: Any,
        **_kwargs: Any,
    ) -> ModelResponse:
        self.inputs.append(input)
        self.system_instructions.append(system_instructions or "")
        match len(self.inputs):
            case 1:
                return _tool_call_response({}, "start_monty", call_id=self.start_call_id)
            case 2:
                self.subagent_handle = _function_output(input, self.start_call_id)
                return _tool_call_response(
                    {
                        "subagent": self.subagent_handle,
                        "message": {"script": _const_script(42)},
                    },
                    "monty_run_script",
                    call_id=self.run_call_id,
                )
            case 3:
                self.run_result_output = _function_output(input, self.run_call_id)
                assert "result: 42" in self.run_result_output
                return ResponseBuilders.output_message("The subagent script returned 42.")
            case _:
                raise AssertionError("unexpected extra OpenAI model call")

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError()


@activity.defn(name=RUN_SUBAGENT_TURN_ACTIVITY)
async def _fake_run_subagent_turn(req: RunSubagentTurnInput) -> SubagentTurnResult:
    async with AgentWorkflowRunner.publisher_from_activity(
        req.parent_stream_context
    ) as publisher:
        publisher.publish(
            SubagentMessageSent(
                subagent_id=req.handle,
                agent_key=req.agent_key,
                workflow_id=req.child_workflow_id,
                function=req.type,
                subagent_turn=req.expected_turn,
                from_offset=req.from_offset,
            )
        )
    return SubagentTurnResult(
        output={"text": "result: 42"},
        turn_id=f"fake-subagent-turn-{req.expected_turn}",
        turn_number=req.expected_turn,
        consumed_offset=req.from_offset + 1,
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


@pytest_asyncio.fixture
async def subagent_client_queue_and_model():
    model = _OpenAISubagentModel()
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=DataConverter(payload_converter_class=OpenAIPayloadConverter)
    )
    task_queue = f"monty-openai-subagent-test-{uuid.uuid4()}"
    async with AgentEnvironment(model=model) as agent_env:
        client = agent_env.applied_on_client(env.client)
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[
                MontyChatOpenAISubagentWorkflow,
                MontyDynamicAgentWorkflow,
            ],
            activities=[
                _fake_run_subagent_turn,
            ],
        ):
            try:
                yield client, task_queue, model
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


async def test_openai_subagent_chat_agent_runs_monty_subagent_tool(
    subagent_client_queue_and_model,
):
    client, task_queue, model = subagent_client_queue_and_model
    handle = await client.start_workflow(
        MontyChatOpenAISubagentWorkflow.run,
        AgentConfig(approval_policy=ToolApprovalPolicy.dangerously_skip_all()),
        id=f"MontyChatOpenAISubagentAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type="ask",
            payload={"text": "Run a script returning 42 through the subagent."},
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

    assert model.subagent_handle
    assert model.run_result_output
    assert "result: 42" in model.run_result_output
    assert "monty_run_script" in model.system_instructions[0]
    assert "run_monty_script" not in model.system_instructions[0]

    event_types = [event.event.type for event in events]
    assert AgentEventType.SUBAGENT_STARTED in event_types
    assert AgentEventType.SUBAGENT_MESSAGE_SENT in event_types
    assert AgentEventType.SUBAGENT_REPLY_RECEIVED in event_types

    reply = next(
        event.event
        for event in events
        if event.event.type == AgentEventType.REPLY
    )
    assert reply.output["text"] == "The subagent script returned 42."


async def test_openai_chat_agent_model_command_choices():
    assert OPENAI_MODEL_OPERATOR_COMMAND.argument is not None
    assert tuple(OPENAI_MODEL_OPERATOR_COMMAND.argument.choices) == OPENAI_SUPPORTED_MODELS
    assert OPENAI_SUPPORTED_MODELS == ("gpt-5.4-mini", "gpt-5.4")
