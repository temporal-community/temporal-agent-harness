"""End-to-end tests for the A2A Nexus binding against a real dev server."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from a2a.types import (
        CancelTaskRequest,
        GetExtendedAgentCardRequest,
        Message,
        Part,
        Role,
        SendMessageRequest,
        StreamResponse,
        TaskState,
    )
    from google.protobuf.json_format import MessageToDict

    from temporal_agent_harness.a2a.adapter import (
        A2AHandlerConfig,
        A2AServiceHandler,
        make_agent_card,
    )
    from temporal_agent_harness.a2a.nexus import A2AService, SubscribeToTaskInput
    from temporal_agent_harness.harness import AgentWorkflowRunner, agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        ToolApprovalPolicy,
    )

_DEV_SERVER_VERSION = "v1.7.1-system-nexus-operations"
_DEV_SERVER_ARGS = [
    "--dynamic-config-value",
    "history.enableChasm=true",
    "--dynamic-config-value",
    "history.enableTransitionHistory=true",
    "--dynamic-config-value",
    "history.enableCHASMCallbacks=true",
    "--dynamic-config-value",
    "history.enableCHASMSignalBacklinks=true",
    "--dynamic-config-value",
    "nexusoperation.enableStandalone=true",
    "--dynamic-config-value",
    'system.system.refreshNexusEndpointsMinWait="0s"',
    "--dynamic-config-value",
    "history.enableUpdateCallbacks=true",
]


class AskMessage(BaseModel):
    """Probe input."""

    text: str


class AskReply(BaseModel):
    """Probe output."""

    text: str


@workflow.defn
@agent.defn
class ProbeAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: AskMessage) -> AskReply:
        """Echo one probe message."""
        await workflow.sleep(0.1)
        return AskReply(text=f"echo: {message.text}")


class CallerInput(BaseModel):
    endpoint: str
    task_id: str


class CallerOutput(BaseModel):
    turn_number: int
    item_count: int
    closed: bool


@workflow.defn(sandboxed=False)
class A2ACallerWorkflow:
    @workflow.run
    async def run(self, input: CallerInput) -> CallerOutput:
        client = workflow.create_nexus_client(
            service=A2AService, endpoint=input.endpoint
        )
        sent = await client.execute_operation(
            A2AService.send_message,
            SendMessageRequest(
                message=Message(
                    message_id=str(workflow.uuid4()),
                    task_id=input.task_id,
                    context_id=input.task_id,
                    role=Role.ROLE_USER,
                    parts=[Part(text="hi")],
                )
            ),
        )
        metadata = MessageToDict(sent.task.metadata)
        cursor = int(metadata["temporal.io/accepted-offset"])
        count = 0
        for _ in range(10):
            page = await client.execute_operation(
                A2AService.subscribe_to_task,
                SubscribeToTaskInput(
                    id=input.task_id, cursor=cursor, timeout_seconds=5
                ),
            )
            count += len(page.items)
            cursor = page.next_cursor
            for item in page.items:
                response = StreamResponse()
                import base64

                response.ParseFromString(base64.b64decode(item.data))
                if (
                    response.HasField("status_update")
                    and response.status_update.status.state
                    == TaskState.TASK_STATE_INPUT_REQUIRED
                ):
                    return CallerOutput(
                        turn_number=int(metadata["temporal.io/turn-number"]),
                        item_count=count,
                        closed=page.closed,
                    )
        raise RuntimeError("A2A turn did not reach input-required")


@workflow.defn(sandboxed=False)
class ReplayCallerWorkflow:
    @workflow.run
    async def run(self, input: CallerInput) -> CallerOutput:
        client = workflow.create_nexus_client(
            service=A2AService, endpoint=input.endpoint
        )
        await client.execute_operation(
            A2AService.cancel_task, CancelTaskRequest(id=input.task_id)
        )
        page = await client.execute_operation(
            A2AService.subscribe_to_task,
            SubscribeToTaskInput(id=input.task_id, cursor=0, timeout_seconds=1),
        )
        return CallerOutput(
            turn_number=0, item_count=len(page.items), closed=page.closed
        )


@workflow.defn(sandboxed=False)
class CardCallerWorkflow:
    @workflow.run
    async def run(self, endpoint: str) -> str:
        client = workflow.create_nexus_client(service=A2AService, endpoint=endpoint)
        card = await client.execute_operation(
            A2AService.get_extended_agent_card, GetExtendedAgentCardRequest()
        )
        return card.name


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_download_version=_DEV_SERVER_VERSION,
        dev_server_extra_args=_DEV_SERVER_ARGS,
    )
    yield env
    await env.shutdown()


async def _run_stack(env: WorkflowEnvironment, caller: type, argument):
    endpoint = f"a2a-agent-{uuid.uuid4()}"
    agent_queue = f"agent-{uuid.uuid4()}"
    nexus_queue = f"nexus-{uuid.uuid4()}"
    caller_queue = f"caller-{uuid.uuid4()}"
    await env.create_nexus_endpoint(endpoint, nexus_queue)
    config = A2AHandlerConfig(
        agent_task_queue=agent_queue,
        workflow_name="ProbeAgent",
        workflow_id_prefix="",
        is_message_queuing_enabled=True,
        agent_card=make_agent_card(
            name="Probe", description="Probe", endpoint=endpoint
        ),
    )
    async with (
        Worker(env.client, task_queue=agent_queue, workflows=[ProbeAgent]),
        Worker(
            env.client,
            task_queue=nexus_queue,
            nexus_service_handlers=[A2AServiceHandler(env.client, config)],
        ),
        Worker(env.client, task_queue=caller_queue, workflows=[caller]),
    ):
        return await env.client.execute_workflow(
            caller.run,
            argument(endpoint) if callable(argument) else argument,
            id=f"caller-{uuid.uuid4()}",
            task_queue=caller_queue,
        )


async def test_a2a_send_and_subscription(env: WorkflowEnvironment) -> None:
    task_id = f"task-{uuid.uuid4()}"
    result = await _run_stack(
        env,
        A2ACallerWorkflow,
        lambda endpoint: CallerInput(endpoint=endpoint, task_id=task_id),
    )
    assert result.turn_number == 1
    assert result.item_count > 0
    assert not result.closed


async def test_agent_card_is_discoverable_over_nexus(env: WorkflowEnvironment) -> None:
    result = await _run_stack(env, CardCallerWorkflow, lambda endpoint: endpoint)
    assert result == "Probe"


async def test_completed_task_history_is_replayable(env: WorkflowEnvironment) -> None:
    task_id = f"task-{uuid.uuid4()}"
    # The first stack starts and completes one turn. A second endpoint would not route
    # to that workflow, so exercise close/replay in one longer-lived worker stack here.
    endpoint = f"a2a-agent-{uuid.uuid4()}"
    agent_queue = f"agent-{uuid.uuid4()}"
    nexus_queue = f"nexus-{uuid.uuid4()}"
    caller_queue = f"caller-{uuid.uuid4()}"
    await env.create_nexus_endpoint(endpoint, nexus_queue)
    config = A2AHandlerConfig(
        agent_task_queue=agent_queue,
        workflow_name="ProbeAgent",
        workflow_id_prefix="",
        is_message_queuing_enabled=True,
        agent_card=make_agent_card(
            name="Probe", description="Probe", endpoint=endpoint
        ),
    )
    async with (
        Worker(env.client, task_queue=agent_queue, workflows=[ProbeAgent]),
        Worker(
            env.client,
            task_queue=nexus_queue,
            nexus_service_handlers=[A2AServiceHandler(env.client, config)],
        ),
        Worker(
            env.client,
            task_queue=caller_queue,
            workflows=[A2ACallerWorkflow, ReplayCallerWorkflow],
        ),
    ):
        await env.client.execute_workflow(
            A2ACallerWorkflow.run,
            CallerInput(endpoint=endpoint, task_id=task_id),
            id=f"send-{uuid.uuid4()}",
            task_queue=caller_queue,
        )
        replay = await env.client.execute_workflow(
            ReplayCallerWorkflow.run,
            CallerInput(endpoint=endpoint, task_id=task_id),
            id=f"replay-{uuid.uuid4()}",
            task_queue=caller_queue,
        )
    assert replay.item_count > 0
    assert replay.closed
