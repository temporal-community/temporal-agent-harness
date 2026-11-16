# ABOUTME: The AgentService Nexus handler: one agent type's sessions, reachable from any
# namespace or SDK language through a Nexus endpoint.
#
# Experimental: the AgentService contract (agent.nexusrpc.yaml) may change between releases.
# Every operation but pollMessages and closeSession delegates to AgentClient (see _agent_client).

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from nexusrpc import HandlerError, HandlerErrorType
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from pydantic import JsonValue, ValidationError
from temporalio import nexus
from temporalio.client import Client, WorkflowUpdateStage
from temporalio.contrib.workflow_streams import PollInput
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    MidTurnRejectedError,
    ToolApprovalError,
)
from temporal_agent_harness.harness.agent_protocol import (
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AutoApprovalCriteria,
    PendingApproval,
    PendingCallback,
    PendingTurn,
    SubagentInfo,
    ToolApprovalPolicy,
)

# Aliased with a Nexus* prefix where the name collides with an agent_protocol type of the
# same name but a different (wire-friendly) shape.
from temporal_agent_harness.nexus_agent_adapter.generated import (
    AcceptedFunction as NexusAcceptedFunction,
    AcceptedFunctionOutput,
    AcceptedFunctionParameters,
    AgentInterfaceOutput,
    AgentStatusOutput,
    ApprovalPolicy as NexusApprovalPolicy,
    ApproveToolCallInput,
    ApproveToolCallOutput,
    CloseSessionOutput,
    PendingApproval as NexusPendingApproval,
    PendingApprovalToolInput,
    PendingCallback as NexusPendingCallback,
    PendingCallbackOutputSchema,
    PendingCallbackToolInput,
    PendingTurn as NexusPendingTurn,
    PollMessagesInput,
    PollMessagesOutput,
    ProvideCallbackResultInput,
    ProvideCallbackResultOutput,
    QuerySessionInput,
    SendAgentMessageInput,
    SendMessageOutput,
    StartConfig,
    SubagentInfo as NexusSubagentInfo,
)
from temporal_agent_harness.nexus_agent_adapter.generated import AgentService as AgentServiceDefinition

# WorkflowStream's private poll-update name (not part of its public API), hardcoded since
# pollMessages must attach to it for any agent without importing that agent's workflow code.
# Each pollMessages attaches a completion callback to the agent's workflow, and the server caps
# the callbacks one workflow accumulates (system.maxCallbacksPerWorkflow, 32 by default).
_WORKFLOW_STREAM_POLL_UPDATE = "__temporal_workflow_stream_poll"

# The harness's close signal (AgentWorkflowRunner registers it on every agent).
_CLOSE_SIGNAL = "close"

# The ApplicationError type behind sendAgentMessage's BAD_REQUEST when the agent is busy.
_MID_TURN_REJECTED = "MidTurnRejected"


def _is_workflow_already_completed(exc: Exception) -> bool:
    """True when the target agent workflow has already finished."""
    return "already completed" in str(exc).lower()


async def _takes_object_result(agent_client: AgentClient, tool_id: str) -> bool:
    """Whether the pending callback ``tool_id`` expects a JSON object as its result.

    An unknown ``tool_id`` counts as an object, so the workflow's own validation reports it.
    """
    for pending in await agent_client.get_pending_callbacks():
        if pending.tool_id == tool_id:
            return pending.output_schema.get("type") == "object"
    return True


def _agent_config(start_config: StartConfig | None) -> AgentConfig:
    """The harness ``AgentConfig`` a caller's ``startConfig`` describes."""
    if start_config is None:
        return AgentConfig()
    policy = start_config.approval_policy
    criteria = start_config.auto_approval_criteria
    idle = start_config.close_after_idle_seconds
    try:
        return AgentConfig(
            approval_policy=None if policy is None else _tool_approval_policy(policy),
            auto_approval_criteria=(
                None
                if criteria is None
                else AutoApprovalCriteria.model_validate(criteria.additional_properties)
            ),
            agent_id=start_config.agent_id,
            close_after_idle=None if idle is None else timedelta(seconds=idle),
        )
    except ValidationError as e:
        raise HandlerError(
            f"invalid startConfig: {e}", type=HandlerErrorType.BAD_REQUEST
        ) from e


def _tool_approval_policy(policy: NexusApprovalPolicy) -> ToolApprovalPolicy:
    return ToolApprovalPolicy(
        dangerously_skip_all_approvals=policy.dangerously_skip_all_approvals,
        auto_approve_inherently_safe=policy.auto_approve_inherently_safe,
        auto_approve_tools=frozenset(policy.auto_approve_tools),
        auto_mode_enabled=policy.auto_mode_enabled,
    )


def _nexus_approval_policy(policy: ToolApprovalPolicy) -> NexusApprovalPolicy:
    return NexusApprovalPolicy(
        dangerously_skip_all_approvals=policy.dangerously_skip_all_approvals,
        auto_approve_inherently_safe=policy.auto_approve_inherently_safe,
        auto_approve_tools=sorted(policy.auto_approve_tools),
        auto_mode_enabled=policy.auto_mode_enabled,
    )


def _nexus_pending_turn(pt: PendingTurn) -> NexusPendingTurn:
    return NexusPendingTurn(
        turn_number=pt.turn_number,
        turn_id=pt.turn_id,
        message_id=pt.message_id,
        message=pt.message,
    )


def _nexus_pending_approval(pa: PendingApproval) -> NexusPendingApproval:
    return NexusPendingApproval(
        tool_id=pa.tool_id,
        short_id=pa.short_id,
        tool_name=pa.tool_name,
        tool_input=PendingApprovalToolInput(additional_properties=pa.tool_input),
        turn_number=pa.turn_number,
    )


def _nexus_pending_callback(pc: PendingCallback) -> NexusPendingCallback:
    return NexusPendingCallback(
        tool_id=pc.tool_id,
        tool_name=pc.tool_name,
        tool_input=PendingCallbackToolInput(additional_properties=pc.tool_input),
        output_schema=PendingCallbackOutputSchema(additional_properties=pc.output_schema),
        turn_number=pc.turn_number,
    )


def _nexus_subagent_info(info: SubagentInfo) -> NexusSubagentInfo:
    return NexusSubagentInfo(
        subagent_id=info.subagent_id,
        agent_key=info.agent_key,
        workflow_id=info.workflow_id,
    )


@dataclass(frozen=True)
class Config:
    """Which agent an AgentServiceHandler serves, and where its sessions run.

    A session's workflow ID is ``workflow_id_prefix`` plus the caller's session ID.
    """

    agent_task_queue: str
    workflow_name: str
    workflow_id_prefix: str


@service_handler(service=AgentServiceDefinition)
class AgentServiceHandler:
    """Serves the AgentService Nexus contract for one agent type.

    Register it on a worker (``nexus_service_handlers=[...]``) whose client uses the harness's
    data converter, e.g. the agent's own worker with ``AgentHarnessPlugin``. A caller starts a
    session and delivers messages with ``sendAgentMessage`` and reads the agent's events with
    ``pollMessages``.
    """

    def __init__(self, client: Client, config: Config) -> None:
        self._client = client
        self._config = config

    def _workflow_id(self, session_id: str) -> str:
        return self._config.workflow_id_prefix + session_id

    def _agent_client(self, session_id: str) -> AgentClient:
        return AgentClient(self._client, self._workflow_id(session_id))

    @sync_operation
    async def send_agent_message(
        self, ctx: StartOperationContext, input: SendAgentMessageInput
    ) -> SendMessageOutput:
        start_config = _agent_config(input.start_config)
        start_data = (
            None if input.start_data is None else input.start_data.additional_properties
        )
        try:
            # The Nexus request ID is the idempotency key: a retried operation re-issues the
            # same update and gets the original acceptance back rather than a second dispatch.
            reply = await self._agent_client(input.session_id).start_and_submit_message(
                input.msg_type,
                input.payload.additional_properties,
                workflow_name=self._config.workflow_name,
                task_queue=self._config.agent_task_queue,
                start_config=start_config,
                start_data=start_data,
                update_id=f"send-{ctx.request_id}",
            )
        except MidTurnRejectedError as e:
            # The contract promises callers this cause's type, so they can tell a busy agent
            # apart from other bad requests.
            raise HandlerError(str(e), type=HandlerErrorType.BAD_REQUEST) from ApplicationError(
                str(e), type=_MID_TURN_REJECTED, non_retryable=True
            )
        return SendMessageOutput(
            turn_number=reply.turn_number,
            turn_id=reply.turn_id,
            message_id=reply.message_id,
            disposition=reply.disposition.value,
            stream_head_offset=reply.accepted_offset,
        )

    @sync_operation
    async def approve_tool_call(
        self, ctx: StartOperationContext, input: ApproveToolCallInput
    ) -> ApproveToolCallOutput:
        try:
            result = await self._agent_client(input.session_id).approve_tool(
                input.tool_id,
                approved=input.approved,
                reason=input.reason,
                remember=input.remember or False,
                update_id=f"approve-{ctx.request_id}",
            )
        except ToolApprovalError as e:
            raise HandlerError(str(e), type=HandlerErrorType.BAD_REQUEST) from e
        return ApproveToolCallOutput(tool_id=result.tool_id, accepted=result.accepted)

    @sync_operation
    async def query_agent_interface(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> AgentInterfaceOutput:
        functions = await self._agent_client(input.session_id).get_agent_interface()
        return AgentInterfaceOutput(
            handlers=[
                NexusAcceptedFunction(
                    name=fn.name,
                    description=fn.description,
                    parameters=AcceptedFunctionParameters(additional_properties=fn.parameters),
                    output=AcceptedFunctionOutput(additional_properties=fn.output),
                    mid_turn=fn.mid_turn.value,
                    model_callable=fn.model_callable,
                )
                for fn in functions
            ]
        )

    @sync_operation
    async def query_agent_status(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> AgentStatusOutput:
        status = await self._agent_client(input.session_id).get_status()
        return AgentStatusOutput(
            agent_id=status.agent_id,
            current_turn=status.current_turn,
            turn_active=status.turn_active,
            turn_participants=status.turn_participants,
            pending_turns=[_nexus_pending_turn(pt) for pt in status.pending_turns],
            pending_approvals=[_nexus_pending_approval(pa) for pa in status.pending_approvals],
            pending_callbacks=[_nexus_pending_callback(pc) for pc in status.pending_callbacks],
            subagents=[_nexus_subagent_info(s) for s in status.subagents],
            approval_policy=_nexus_approval_policy(status.approval_policy),
            has_auto_approval_evaluator=status.has_auto_approval_evaluator,
            close_reason=None if status.close_reason is None else status.close_reason.value,
        )

    @sync_operation
    async def provide_callback_result(
        self, ctx: StartOperationContext, input: ProvideCallbackResultInput
    ) -> ProvideCallbackResultOutput:
        agent_client = self._agent_client(input.session_id)
        callback_result: JsonValue = None
        if input.result is not None:
            callback_result = input.result.additional_properties
            # The contract carries a scalar or list result wrapped as {"value": ...}; unwrap it
            # for a tool whose output isn't an object.
            if not await _takes_object_result(agent_client, input.tool_id):
                callback_result = callback_result.get("value")
        try:
            result = await agent_client.provide_callback_result(
                input.tool_id,
                result=callback_result,
                error=input.error,
                update_id=f"callback-{ctx.request_id}",
            )
        except CallbackResultError as e:
            raise HandlerError(str(e), type=HandlerErrorType.BAD_REQUEST) from e
        return ProvideCallbackResultOutput(tool_id=result.tool_id, accepted=result.accepted)

    @sync_operation
    async def close_session(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> CloseSessionOutput:
        handle = self._client.get_workflow_handle(self._workflow_id(input.session_id))
        try:
            await handle.signal(_CLOSE_SIGNAL)
        except RPCError as e:
            if _is_workflow_already_completed(e):
                return CloseSessionOutput(already_closed=True)
            if e.status == RPCStatusCode.NOT_FOUND:
                raise HandlerError(
                    f"no session {input.session_id!r}", type=HandlerErrorType.NOT_FOUND
                ) from e
            raise
        return CloseSessionOutput(already_closed=False)

    @nexus.temporal_operation
    async def poll_messages(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: PollMessagesInput,
    ) -> nexus.TemporalOperationResult[PollMessagesOutput]:
        """Attaches to WorkflowStream's poll update with a completion callback, so the
        update's result completes the operation. Returns closed=True synchronously when the
        target workflow has already completed.
        """
        try:
            return await client.start_workflow_update(
                self._workflow_id(input.session_id),
                _WORKFLOW_STREAM_POLL_UPDATE,
                PollInput(from_offset=input.cursor, topics=[TURN_EVENTS_TOPIC]),
                wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                result_type=PollMessagesOutput,
            )
        except RPCError as e:
            if not _is_workflow_already_completed(e):
                raise
            return nexus.TemporalOperationResult.sync(
                PollMessagesOutput(
                    items=[], more_ready=False, next_offset=input.cursor, closed=True
                )
            )
