# ABOUTME: End-to-end integration tests for AgentServiceHandler against a real dev server.
#
# Needs a real dev server, not the time-skipping test server: update-with-callback requires
# dynamic config the time-skipping server doesn't have (see _DEV_SERVER_ARGS below).
#
# Run with: bazel test //python/tests/nexus_agent_adapter:test_handler_integration

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from pydantic import BaseModel
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import base64
    import json

    from temporalio.api.common.v1 import Payload

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_ID_LENGTH,
    AgentConfig,
    MidTurn,
    ToolApprovalPolicy,
)
from temporal_agent_harness.nexus_agent_adapter.generated import (
    AgentService as AgentServiceDefinition,
)
from temporal_agent_harness.nexus_agent_adapter.generated import (
    ApproveToolCallInput,
    PollMessagesInput,
    QuerySessionInput,
    SendAgentMessageInput,
    SendAgentMessageInputPayload,
    SendAgentMessageInputStartData,
    StartConfig,
)
from temporal_agent_harness.nexus_agent_adapter.handler import AgentServiceHandler, Config

_DEV_SERVER_ARGS = [
    "--dynamic-config-value",
    "history.enableUpdateCallbacks=true",
    # Endpoint registration is cached; 0s keeps create_nexus_endpoint visible immediately.
    "--dynamic-config-value",
    'system.system.refreshNexusEndpointsMinWait="0s"',
]

# A Nexus handler that raises is retried rather than failed, so an unbounded result() would hang.
_RESULT_TIMEOUT_SECONDS = 30


class AskMessage(BaseModel):
    """A user message to the probe agent."""

    text: str


class AskReply(BaseModel):
    """The probe agent's reply."""

    text: str


@agent.defn
class ProbeAgent:
    """2s reply delay, so pollMessages is still pending when it starts (the async path).
    ``ask`` queues mid-turn, so a second message during that delay opens the next turn."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts(mid_turn=MidTurn.ENQUEUE)
    async def ask(self, message: AskMessage) -> AskReply:
        """Reply to a user message."""
        await workflow.sleep(2)
        return AskReply(text=f"echo: {message.text}")


class CallerInput(BaseModel):
    endpoint: str
    session_id: str


class CallerOutput(BaseModel):
    turn_number: int
    polls: int
    reply: dict[str, str]


def _decode_event(data: str) -> dict:
    """A stream item's data: base64 of a serialized Payload whose data is the event's JSON."""
    return json.loads(Payload.FromString(base64.b64decode(data)).data)


@workflow.defn
class CallerWorkflow:
    """Stands in for ui_connector: sends a message, then polls until its reply arrives."""

    @workflow.run
    async def run(self, input: CallerInput) -> CallerOutput:
        client = workflow.create_nexus_client(
            service=AgentServiceDefinition, endpoint=input.endpoint
        )
        send_out = await client.execute_operation(
            AgentServiceDefinition.send_agent_message,
            SendAgentMessageInput(
                session_id=input.session_id,
                msg_type="ask",
                payload=SendAgentMessageInputPayload(additional_properties={"text": "hi"}),
            ),
        )
        cursor, polls = send_out.stream_head_offset, 0
        while True:
            polls += 1
            poll_out = await client.execute_operation(
                AgentServiceDefinition.poll_messages,
                PollMessagesInput(session_id=input.session_id, cursor=cursor),
            )
            assert not poll_out.closed
            cursor = poll_out.next_offset
            for item in poll_out.items:
                envelope = _decode_event(item.data)
                if (
                    envelope["message_id"] == send_out.message_id
                    and envelope["event"]["type"] == "message_handler_end"
                ):
                    return CallerOutput(
                        turn_number=send_out.turn_number,
                        polls=polls,
                        reply=envelope["event"]["output"],
                    )


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_extra_args=_DEV_SERVER_ARGS,
    )
    yield env
    await env.shutdown()


async def test_poll_messages_delivers_the_reply_via_async_callback(env: WorkflowEnvironment) -> None:
    client = env.client
    endpoint_name = f"agent-endpoint-{uuid.uuid4()}"
    agent_task_queue = f"agent-{uuid.uuid4()}"
    nexus_task_queue = f"nexus-agent-{uuid.uuid4()}"
    caller_task_queue = f"caller-{uuid.uuid4()}"

    await env.create_nexus_endpoint(endpoint_name, nexus_task_queue)

    config = Config(
        agent_task_queue=agent_task_queue,
        workflow_name="ProbeAgent",
        workflow_id_prefix="probe-",
    )

    async with Worker(
        client,
        task_queue=agent_task_queue,
        workflows=[ProbeAgent],
    ), Worker(
        client,
        task_queue=nexus_task_queue,
        nexus_service_handlers=[AgentServiceHandler(client, config)],
    ), Worker(
        client,
        task_queue=caller_task_queue,
        workflows=[CallerWorkflow],
    ):
        session_id = str(uuid.uuid4())
        handle = await client.start_workflow(
            CallerWorkflow.run,
            CallerInput(endpoint=endpoint_name, session_id=session_id),
            id=f"caller-{session_id}",
            task_queue=caller_task_queue,
        )
        result = await asyncio.wait_for(handle.result(), _RESULT_TIMEOUT_SECONDS)

        assert result.turn_number == 1
        assert result.reply == {"text": "echo: hi"}

        # Proves pollMessages completed asynchronously, through its callback.
        history = await handle.fetch_history()
        op_types = {e.event_type for e in history.events}
        assert EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED in op_types
        assert EventType.EVENT_TYPE_NEXUS_OPERATION_COMPLETED in op_types


class SendOnlyOutput(BaseModel):
    turn_number: int


@workflow.defn
class SendOnlyCallerWorkflow:
    """Send-only, no poll — avoids racing the probe agent's reply delay across a restart."""

    @workflow.run
    async def run(self, input: CallerInput) -> SendOnlyOutput:
        client = workflow.create_nexus_client(
            service=AgentServiceDefinition, endpoint=input.endpoint
        )
        send_out = await client.execute_operation(
            AgentServiceDefinition.send_agent_message,
            SendAgentMessageInput(
                session_id=input.session_id,
                msg_type="ask",
                payload=SendAgentMessageInputPayload(additional_properties={"text": "hi"}),
            ),
        )
        return SendOnlyOutput(turn_number=send_out.turn_number)


async def test_send_agent_message_survives_handler_worker_restart(
    env: WorkflowEnvironment,
) -> None:
    """Handler worker restarts between two sends on the same session; turn count still
    advances — state lives in the workflow, not the handler."""
    client = env.client
    endpoint_name = f"agent-endpoint-{uuid.uuid4()}"
    agent_task_queue = f"agent-{uuid.uuid4()}"
    nexus_task_queue = f"nexus-agent-{uuid.uuid4()}"
    caller_task_queue = f"caller-{uuid.uuid4()}"

    await env.create_nexus_endpoint(endpoint_name, nexus_task_queue)

    config = Config(
        agent_task_queue=agent_task_queue,
        workflow_name="ProbeAgent",
        workflow_id_prefix="probe-",
    )
    session_id = str(uuid.uuid4())

    async with Worker(
        client, task_queue=agent_task_queue, workflows=[ProbeAgent]
    ), Worker(
        client, task_queue=caller_task_queue, workflows=[SendOnlyCallerWorkflow]
    ):
        handler_worker_0 = Worker(
            client,
            task_queue=nexus_task_queue,
            nexus_service_handlers=[AgentServiceHandler(client, config)],
        )
        async with handler_worker_0:
            handle_1 = await client.start_workflow(
                SendOnlyCallerWorkflow.run,
                CallerInput(endpoint=endpoint_name, session_id=session_id),
                id=f"caller-restart-msg1-{session_id}",
                task_queue=caller_task_queue,
            )
            result_1 = await asyncio.wait_for(handle_1.result(), _RESULT_TIMEOUT_SECONDS)
        assert result_1.turn_number == 1

        handler_worker_1 = Worker(
            client,
            task_queue=nexus_task_queue,
            nexus_service_handlers=[AgentServiceHandler(client, config)],
        )
        async with handler_worker_1:
            handle_2 = await client.start_workflow(
                SendOnlyCallerWorkflow.run,
                CallerInput(endpoint=endpoint_name, session_id=session_id),
                id=f"caller-restart-msg2-{session_id}",
                task_queue=caller_task_queue,
            )
            result_2 = await asyncio.wait_for(handle_2.result(), _RESULT_TIMEOUT_SECONDS)
        assert result_2.turn_number == 2, (
            "turn counter must advance after a handler worker restart"
        )


class PollOnlyOutput(BaseModel):
    poll_closed: bool
    poll_item_count: int


@workflow.defn
class PollOnlyCallerWorkflow:
    """Calls only pollMessages, targeting a session whose workflow has already completed."""

    @workflow.run
    async def run(self, input: CallerInput) -> PollOnlyOutput:
        client = workflow.create_nexus_client(
            service=AgentServiceDefinition, endpoint=input.endpoint
        )
        poll_out = await client.execute_operation(
            AgentServiceDefinition.poll_messages,
            PollMessagesInput(session_id=input.session_id, cursor=0),
        )
        return PollOnlyOutput(
            poll_closed=bool(poll_out.closed), poll_item_count=len(poll_out.items)
        )


@workflow.defn
class ImmediatelyDoneWorkflow:
    @workflow.run
    async def run(self) -> None:
        return None


async def test_poll_messages_closed_when_workflow_already_completed(
    env: WorkflowEnvironment,
) -> None:
    """A completed target workflow must produce closed=True synchronously, not an error."""
    client = env.client
    endpoint_name = f"agent-endpoint-{uuid.uuid4()}"
    nexus_task_queue = f"nexus-agent-{uuid.uuid4()}"
    completed_task_queue = f"completed-{uuid.uuid4()}"
    caller_task_queue = f"caller-{uuid.uuid4()}"

    await env.create_nexus_endpoint(endpoint_name, nexus_task_queue)

    config = Config(
        agent_task_queue=completed_task_queue,
        workflow_name="ImmediatelyDoneWorkflow",
        workflow_id_prefix="done-",
    )

    async with Worker(
        client,
        task_queue=completed_task_queue,
        workflows=[ImmediatelyDoneWorkflow],
    ), Worker(
        client,
        task_queue=nexus_task_queue,
        nexus_service_handlers=[AgentServiceHandler(client, config)],
    ), Worker(
        client,
        task_queue=caller_task_queue,
        workflows=[PollOnlyCallerWorkflow],
    ):
        session_id = str(uuid.uuid4())
        workflow_id = "done-" + session_id
        await client.execute_workflow(
            ImmediatelyDoneWorkflow.run,
            id=workflow_id,
            task_queue=completed_task_queue,
        )

        handle = await client.start_workflow(
            PollOnlyCallerWorkflow.run,
            CallerInput(endpoint=endpoint_name, session_id=session_id),
            id=f"poll-only-caller-{session_id}",
            task_queue=caller_task_queue,
        )
        result = await asyncio.wait_for(handle.result(), _RESULT_TIMEOUT_SECONDS)

        assert result.poll_closed
        assert result.poll_item_count == 0


# ---------------------------------------------------------------------------
# Full operation surface: sendAgentMessage, queryAgentInterface, queryAgentStatus,
# approveToolCall, pollMessages, closeSession.
# ---------------------------------------------------------------------------

_AGENT_ID = "b" * AGENT_ID_LENGTH


@agent.tool_defn()
async def gated_tool(text: str) -> str:
    """A non-safe inline tool: gated under the default policy."""
    return f"tool-result:{text}"


@agent.defn
class GatedProbeAgent:
    """Gates every tool call, unlike ProbeAgent — needed to exercise approveToolCall."""

    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_human_approval(),
        )

    @agent.accepts
    async def use_tool(self, message: AskMessage) -> AskReply:
        """Run a gated tool call and reply with its result."""
        result = await self._runner.run_tool("fixed-tool-id", gated_tool, message.text)
        return AskReply(text=result)


class FullSurfaceOutput(BaseModel):
    handler_names: list[str]
    handler_mid_turns: list[str]
    message_id: str
    disposition: str
    status_agent_id: str
    pending_tool_id: str
    pending_short_id: str
    approve_accepted: bool
    poll_item_count: int
    already_closed: bool


@workflow.defn
class FullSurfaceCallerWorkflow:
    """Exercises every operation over the Nexus wire."""

    @workflow.run
    async def run(self, input: CallerInput) -> FullSurfaceOutput:
        client = workflow.create_nexus_client(
            service=AgentServiceDefinition, endpoint=input.endpoint
        )

        # Must go first — starts the workflow the queries below need.
        send_out = await client.execute_operation(
            AgentServiceDefinition.send_agent_message,
            SendAgentMessageInput(
                session_id=input.session_id,
                msg_type="use_tool",
                payload=SendAgentMessageInputPayload(additional_properties={"text": "hi"}),
                start_config=StartConfig(agent_id=_AGENT_ID),
            ),
        )

        iface = await client.execute_operation(
            AgentServiceDefinition.query_agent_interface,
            QuerySessionInput(session_id=input.session_id),
        )

        # Poll instead of sleep — the approval record lands asynchronously.
        status = None
        for _ in range(20):
            status = await client.execute_operation(
                AgentServiceDefinition.query_agent_status,
                QuerySessionInput(session_id=input.session_id),
            )
            if status.pending_approvals:
                break
            await workflow.sleep(0.1)
        assert status is not None and status.pending_approvals
        tool_id = status.pending_approvals[0].tool_id
        short_id = status.pending_approvals[0].short_id

        approve_out = await client.execute_operation(
            AgentServiceDefinition.approve_tool_call,
            ApproveToolCallInput(
                session_id=input.session_id, tool_id=tool_id, approved=True
            ),
        )

        poll_out = await client.execute_operation(
            AgentServiceDefinition.poll_messages,
            PollMessagesInput(
                session_id=input.session_id,
                cursor=send_out.stream_head_offset or 0,
            ),
        )

        close_out = await client.execute_operation(
            AgentServiceDefinition.close_session,
            QuerySessionInput(session_id=input.session_id),
        )

        return FullSurfaceOutput(
            handler_names=sorted(h.name for h in iface.handlers),
            handler_mid_turns=[h.mid_turn for h in sorted(iface.handlers, key=lambda h: h.name)],
            message_id=send_out.message_id,
            disposition=send_out.disposition,
            status_agent_id=status.agent_id,
            pending_tool_id=tool_id,
            pending_short_id=short_id,
            approve_accepted=approve_out.accepted,
            poll_item_count=len(poll_out.items),
            already_closed=close_out.already_closed,
        )


class MalformedCallOutput(BaseModel):
    error_type: str
    error_message: str


@workflow.defn
class MalformedInputCallerWorkflow:
    """Sends sendAgentMessage input without its required sessionId, untyped, the way a
    caller in another language could. The generated Python types would refuse to send it, so
    this reaches the handler's own input validation."""

    @workflow.run
    async def run(self, input: CallerInput) -> MalformedCallOutput:
        client = workflow.create_nexus_client(service="AgentService", endpoint=input.endpoint)
        try:
            await client.execute_operation(
                "SendAgentMessage", {"msgType": "ask", "payload": {"text": "hi"}}
            )
        except Exception as e:
            chain = [f"{type(e).__name__}: {e}"]
            # Protobuf singular message fields are never None when unset (they return
            # an empty message), so walk with HasField instead of an is-None check.
            failure = getattr(e, "failure", None)
            while failure is not None and failure.HasField("cause"):
                failure = failure.cause
                chain.append(f"failure.cause: {failure.message}")
                # A payload validation failure lists its violations in its details.
                for detail in failure.application_failure_info.details.payloads:
                    chain.append(f"detail: {detail.data.decode()}")
            return MalformedCallOutput(
                error_type=type(e).__name__, error_message=" | ".join(chain)
            )
        raise AssertionError("malformed input should not have succeeded")


async def test_send_agent_message_rejects_malformed_input(
    env: WorkflowEnvironment,
) -> None:
    """A required field sent as null over the wire must fail the Nexus call, not crash
    the handler worker or silently proceed with a bad value."""
    client = env.client
    endpoint_name = f"agent-endpoint-{uuid.uuid4()}"
    agent_task_queue = f"agent-{uuid.uuid4()}"
    nexus_task_queue = f"nexus-agent-{uuid.uuid4()}"
    caller_task_queue = f"caller-{uuid.uuid4()}"

    await env.create_nexus_endpoint(endpoint_name, nexus_task_queue)

    # No agent workflow worker needed: input conversion fails before the handler's
    # send_agent_message body (and therefore AgentClient) ever runs.
    config = Config(
        agent_task_queue=agent_task_queue,
        workflow_name="ProbeAgent",
        workflow_id_prefix="probe-",
    )

    async with Worker(
        client,
        task_queue=nexus_task_queue,
        nexus_service_handlers=[AgentServiceHandler(client, config)],
    ), Worker(
        client,
        task_queue=caller_task_queue,
        workflows=[MalformedInputCallerWorkflow],
    ):
        session_id = str(uuid.uuid4())
        handle = await client.start_workflow(
            MalformedInputCallerWorkflow.run,
            CallerInput(endpoint=endpoint_name, session_id=session_id),
            id=f"malformed-caller-{session_id}",
            task_queue=caller_task_queue,
        )
        result = await asyncio.wait_for(handle.result(), _RESULT_TIMEOUT_SECONDS)

        assert result.error_type == "NexusOperationError"
        assert "sessionId" in result.error_message
        assert "required" in result.error_message


async def test_full_operation_surface(env: WorkflowEnvironment) -> None:
    client = env.client
    endpoint_name = f"agent-endpoint-{uuid.uuid4()}"
    agent_task_queue = f"agent-{uuid.uuid4()}"
    nexus_task_queue = f"nexus-agent-{uuid.uuid4()}"
    caller_task_queue = f"caller-{uuid.uuid4()}"

    await env.create_nexus_endpoint(endpoint_name, nexus_task_queue)

    config = Config(
        agent_task_queue=agent_task_queue,
        workflow_name="GatedProbeAgent",
        workflow_id_prefix="gated-probe-",
    )

    async with Worker(
        client,
        task_queue=agent_task_queue,
        workflows=[GatedProbeAgent],
    ), Worker(
        client,
        task_queue=nexus_task_queue,
        nexus_service_handlers=[AgentServiceHandler(client, config)],
    ), Worker(
        client,
        task_queue=caller_task_queue,
        workflows=[FullSurfaceCallerWorkflow],
    ):
        session_id = str(uuid.uuid4())
        handle = await client.start_workflow(
            FullSurfaceCallerWorkflow.run,
            CallerInput(endpoint=endpoint_name, session_id=session_id),
            id=f"full-surface-caller-{session_id}",
            task_queue=caller_task_queue,
        )
        result = await asyncio.wait_for(handle.result(), _RESULT_TIMEOUT_SECONDS)

        assert result.handler_names == ["use_tool"]
        assert result.handler_mid_turns == ["reject"]
        assert result.message_id
        assert result.disposition == "opened"
        assert result.status_agent_id == _AGENT_ID
        assert result.pending_tool_id == "fixed-tool-id"
        assert result.pending_short_id
        assert result.approve_accepted
        assert result.poll_item_count > 0
        assert not result.already_closed

        agent_handle = client.get_workflow_handle(f"gated-probe-{session_id}")
        await asyncio.wait_for(agent_handle.result(), _RESULT_TIMEOUT_SECONDS)


class Greeting(BaseModel):
    """The init data GreetingProbeAgent requires."""

    prefix: str


@agent.defn
class GreetingProbeAgent:
    """Requires init data, so it only starts when sendAgentMessage carries startData."""

    @agent.init
    def __init__(self, config: AgentConfig, data: Greeting) -> None:
        self._prefix = data.prefix
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def ask(self, message: AskMessage) -> AskReply:
        """Reply to a user message."""
        return AskReply(text=f"{self._prefix}{message.text}")


@workflow.defn
class StartDataCallerWorkflow:
    """Sends one message that starts GreetingProbeAgent with a config and init data."""

    @workflow.run
    async def run(self, input: CallerInput) -> SendOnlyOutput:
        client = workflow.create_nexus_client(
            service=AgentServiceDefinition, endpoint=input.endpoint
        )
        send_out = await client.execute_operation(
            AgentServiceDefinition.send_agent_message,
            SendAgentMessageInput(
                session_id=input.session_id,
                msg_type="ask",
                payload=SendAgentMessageInputPayload(additional_properties={"text": "hi"}),
                start_config=StartConfig(agent_id=_AGENT_ID, close_after_idle_seconds=30),
                start_data=SendAgentMessageInputStartData(
                    additional_properties={"prefix": "hello, "}
                ),
            ),
        )
        return SendOnlyOutput(turn_number=send_out.turn_number)


async def test_send_agent_message_starts_the_agent_with_start_config_and_data(
    env: WorkflowEnvironment,
) -> None:
    client = env.client
    endpoint_name = f"agent-endpoint-{uuid.uuid4()}"
    agent_task_queue = f"agent-{uuid.uuid4()}"
    nexus_task_queue = f"nexus-agent-{uuid.uuid4()}"
    caller_task_queue = f"caller-{uuid.uuid4()}"

    await env.create_nexus_endpoint(endpoint_name, nexus_task_queue)

    config = Config(
        agent_task_queue=agent_task_queue,
        workflow_name="GreetingProbeAgent",
        workflow_id_prefix="greeting-probe-",
    )

    async with Worker(
        client, task_queue=agent_task_queue, workflows=[GreetingProbeAgent]
    ), Worker(
        client,
        task_queue=nexus_task_queue,
        nexus_service_handlers=[AgentServiceHandler(client, config)],
    ), Worker(
        client, task_queue=caller_task_queue, workflows=[StartDataCallerWorkflow]
    ):
        session_id = str(uuid.uuid4())
        handle = await client.start_workflow(
            StartDataCallerWorkflow.run,
            CallerInput(endpoint=endpoint_name, session_id=session_id),
            id=f"start-data-caller-{session_id}",
            task_queue=caller_task_queue,
        )
        result = await asyncio.wait_for(handle.result(), _RESULT_TIMEOUT_SECONDS)
        assert result.turn_number == 1

        agent_history = await client.get_workflow_handle(
            f"greeting-probe-{session_id}"
        ).fetch_history()
        started = agent_history.events[0].workflow_execution_started_event_attributes
        start_config, start_data = client.data_converter.payload_converter.from_payloads(
            list(started.input.payloads), [AgentConfig, Greeting]
        )
        assert start_config.agent_id == _AGENT_ID
        assert start_config.close_after_idle is not None
        assert start_config.close_after_idle.total_seconds() == 30
        assert start_data == Greeting(prefix="hello, ")
