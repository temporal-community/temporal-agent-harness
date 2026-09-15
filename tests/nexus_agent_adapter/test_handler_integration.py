# ABOUTME: End-to-end integration tests for AgentServiceHandler against a real dev server.
#
# Needs a real dev server, not the time-skipping test server — update-with-callback requires
# dynamic config the time-skipping server doesn't have (see _DEV_SERVER_ARGS below).
#
# Run with: uv run pytest tests/nexus_agent_adapter/test_handler_integration.py -v

from __future__ import annotations

import json
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

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import AgentConfig, ToolApprovalPolicy
from temporal_agent_harness.nexus_agent_adapter.generated import (
    AgentService as AgentServiceDefinition,
)
from temporal_agent_harness.nexus_agent_adapter.generated import (
    ApproveToolCallInput,
    PollMessagesInput,
    QuerySessionInput,
    SendAgentMessageInput,
)
from temporal_agent_harness.nexus_agent_adapter.handler import AgentServiceHandler, Config

# Custom dev-server build with the Nexus-update-callback dynamic config surface (matches
# sdk-python's own tests/conftest.py for PR #1631 — the stock time-skipping test server and
# ordinary dev-server releases don't have these flags).
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
    """A user message to the probe agent."""

    text: str


class AskReply(BaseModel):
    """The probe agent's reply."""

    text: str


@workflow.defn
@agent.defn
class ProbeAgent:
    """2s reply delay so pollMessages is provably still pending (async path, not sync)."""

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
        """Reply to a user message."""
        await workflow.sleep(2)
        return AskReply(text=f"echo: {message.text}")


class CallerInput(BaseModel):
    endpoint: str
    session_id: str


class CallerOutput(BaseModel):
    turn_id: str
    turn_number: int
    poll_closed: bool
    poll_item_count: int


@workflow.defn
class CallerWorkflow:
    """Stands in for ui_connector — calls AgentService purely over the Nexus wire."""

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
                payload=json.dumps({"text": "hi"}),
            ),
        )
        poll_out = await client.execute_operation(
            AgentServiceDefinition.poll_messages,
            PollMessagesInput(
                session_id=input.session_id,
                cursor=send_out.stream_head_offset or 0,
                timeout_seconds=20,
            ),
        )
        return CallerOutput(
            turn_id=send_out.turn_id,
            turn_number=send_out.turn_number,
            poll_closed=bool(poll_out.closed),
            poll_item_count=len(poll_out.items),
        )


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_download_version=_DEV_SERVER_VERSION,
        dev_server_extra_args=_DEV_SERVER_ARGS,
    )
    yield env
    await env.shutdown()


async def test_poll_messages_delivers_via_async_callback(env: WorkflowEnvironment) -> None:
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
        result = await handle.result()

        assert result.turn_number == 1
        assert not result.poll_closed
        assert result.poll_item_count > 0, (
            "pollMessages must deliver the reply's stream items"
        )

        # Proves the async callback path was taken, not a sync completion.
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
                payload=json.dumps({"text": "hi"}),
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
            result_1 = await handle_1.result()
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
            result_2 = await handle_2.result()
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
        result = await handle.result()

        assert result.poll_closed
        assert result.poll_item_count == 0


# ---------------------------------------------------------------------------
# Full operation surface — sendAgentMessage, queryAgentInterface, queryAgentStatus,
# approveToolCall, pollMessages. (executeOperatorCommand / queryOperatorInterface are
# NOT exercised: operator commands are gone from the harness and those two operations now
# answer NOT_IMPLEMENTED until the contract is regenerated without them.)
# ---------------------------------------------------------------------------


@agent.tool_defn()
async def gated_tool(text: str) -> str:
    """A non-safe inline tool: gated under the default policy."""
    return f"tool-result:{text}"


@workflow.defn
@agent.defn
class GatedProbeAgent:
    """Gates every tool call, unlike ProbeAgent — needed to exercise approveToolCall."""

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
        )

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def use_tool(self, message: AskMessage) -> AskReply:
        """Run a gated tool call and reply with its result."""
        result = await self._runner.run_tool("fixed-tool-id", gated_tool, message.text)
        return AskReply(text=result)


class FullSurfaceOutput(BaseModel):
    handler_names: list[str]
    pending_tool_id: str
    approve_accepted: bool
    poll_item_count: int


@workflow.defn
class FullSurfaceCallerWorkflow:
    """Exercises the remaining operations: interface/status queries, approveToolCall,
    pollMessages."""

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
                payload=json.dumps({"text": "hi"}),
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
                timeout_seconds=20,
            ),
        )

        return FullSurfaceOutput(
            handler_names=sorted(h.name for h in iface.handlers),
            pending_tool_id=tool_id,
            approve_accepted=approve_out.accepted,
            poll_item_count=len(poll_out.items),
        )


class MalformedCallOutput(BaseModel):
    error_type: str
    error_message: str


@workflow.defn
class MalformedInputCallerWorkflow:
    """Sends session_id=None for a required str field. The generated dataclass has no
    constructor-time validation (unlike the old pydantic models), so this only gets
    caught by TransferTypeConverter.from_transfer_type on the handler side, once the
    value crosses the wire. Proves that boundary still rejects malformed input."""

    @workflow.run
    async def run(self, input: CallerInput) -> MalformedCallOutput:
        client = workflow.create_nexus_client(
            service=AgentServiceDefinition, endpoint=input.endpoint
        )
        try:
            await client.execute_operation(
                AgentServiceDefinition.send_agent_message,
                SendAgentMessageInput(
                    session_id=None,  # type: ignore[arg-type]
                    msg_type="ask",
                    payload=json.dumps({"text": "hi"}),
                ),
            )
        except Exception as e:
            chain = [f"{type(e).__name__}: {e}"]
            # Protobuf singular message fields are never None when unset (they return
            # an empty message), so walk with HasField instead of an is-None check.
            failure = getattr(e, "failure", None)
            while failure is not None and failure.HasField("cause"):
                failure = failure.cause
                chain.append(f"failure.cause: {failure.message}")
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
        result = await handle.result()

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
        result = await handle.result()

        assert result.handler_names == ["use_tool"]
        assert result.pending_tool_id == "fixed-tool-id"
        assert result.approve_accepted
        assert result.poll_item_count > 0
