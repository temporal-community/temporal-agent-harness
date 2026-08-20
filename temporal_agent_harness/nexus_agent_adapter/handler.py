# ABOUTME: Python implementation of the AgentService Nexus handler.
#
# Replaces a Go handler (see git history) that only existed because pollMessages needs
# update-with-callback, unsupported in Python until sdk-python#1631. All operations except
# pollMessages just delegate to AgentClient (see _agent_client).

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from nexusrpc import HandlerError, HandlerErrorType
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from temporalio import nexus
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.contrib.workflow_streams import PollInput, PollResult
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    StaleTurnError,
    ToolApprovalError,
)
from temporal_agent_harness.harness.agent_protocol import (
    TURN_EVENTS_TOPIC,
    AgentConfig,
    OperatorCommand,
    PendingCallback,
    PendingTurn,
    SubagentInfo,
    ToolApprovalPolicy,
)

# Aliased with a Nexus* prefix where the name collides with an agent_protocol type of the
# same name but a different (reshaped, wire-friendly) shape.
from .generated import (
    AcceptedFunction as NexusAcceptedFunction,
    AgentInterfaceOutput,
    ApprovalPolicy as NexusApprovalPolicy,
    AgentStatusOutput,
    ApproveToolCallInput,
    ApproveToolCallOutput,
    CloseSessionOutput,
    DescribeSessionOutput,
    DiscoverSessionsOutput,
    EmptyInput,
    ExecuteOperatorCommandInput,
    ExecuteOperatorCommandOutput,
    OperatorCommand as NexusOperatorCommand,
    OperatorCommandArgument as NexusOperatorCommandArgument,
    PendingApproval as NexusPendingApproval,
    PendingCallback as NexusPendingCallback,
    PendingTurn as NexusPendingTurn,
    PollMessagesInput,
    PollMessagesOutput,
    ProvideCallbackResultInput,
    ProvideCallbackResultOutput,
    QueryOperatorInterfaceOutput,
    QuerySessionInput,
    SendAgentMessageInput,
    SendMessageOutput,
    SessionSummary as NexusSessionSummary,
    StreamItem,
    SubagentInfo as NexusSubagentInfo,
)
from .generated import AgentService as AgentServiceDefinition

_DISCOVERY_LIMIT = 200

# WorkflowStream's private poll-update name (not part of its public API), hardcoded since
# pollMessages must attach to it for any agent without importing that agent's workflow code.
_WORKFLOW_STREAM_POLL_UPDATE = "__temporal_workflow_stream_poll"
DEFAULT_POLL_TIMEOUT_SECONDS = 30.0
_MAX_SEND_RETRIES = 5


def _is_workflow_already_completed(exc: Exception) -> bool:
    """True when the target agent workflow has already finished (see poll_messages)."""
    return "already completed" in str(exc).lower()


def _is_workflow_not_found(exc: Exception) -> bool:
    """True when the target session has no workflow yet (not started, no message sent).
    Checked both ways: describe/query-style calls fail with a clean RPCStatusCode.NOT_FOUND;
    start_workflow_update (see poll_messages) surfaces it as a message string instead, same
    as _is_workflow_already_completed does for that same call."""
    if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
        return True
    return "workflow not found" in str(exc).lower()


def _nexus_operator_command(cmd: OperatorCommand) -> NexusOperatorCommand:
    # nex-gen models reject an optional field set to None — omit the kwarg instead.
    argument_kwargs: dict[str, object] = {}
    if cmd.argument is not None:
        arg_kwargs: dict[str, object] = dict(
            kind=cmd.argument.kind,
            required=cmd.argument.required,
            choices=list(cmd.argument.choices),
            allow_multiple=cmd.argument.allow_multiple,
        )
        if cmd.argument.placeholder is not None:
            arg_kwargs["placeholder"] = cmd.argument.placeholder
        argument_kwargs["argument"] = NexusOperatorCommandArgument(**arg_kwargs)
    return NexusOperatorCommand(
        name=cmd.name,
        label=cmd.label,
        description=cmd.description,
        source=cmd.source,
        **argument_kwargs,
    )


def _nexus_pending_callback(pc: PendingCallback) -> NexusPendingCallback:
    return NexusPendingCallback(
        tool_id=pc.tool_id,
        tool_name=pc.tool_name,
        tool_input=json.dumps(pc.tool_input),
        output_schema=json.dumps(pc.output_schema),
        turn_number=pc.turn_number,
    )


def _nexus_pending_turn(pt: PendingTurn) -> NexusPendingTurn:
    return NexusPendingTurn(
        turn_number=pt.turn_number, turn_id=pt.turn_id, message=pt.message
    )


def _nexus_subagent_info(info: SubagentInfo) -> NexusSubagentInfo:
    return NexusSubagentInfo(
        subagent_id=info.subagent_id,
        agent_key=info.agent_key,
        workflow_id=info.workflow_id,
        next_expected_turn=info.next_expected_turn,
    )


def _nexus_approval_policy(policy: ToolApprovalPolicy) -> NexusApprovalPolicy:
    return NexusApprovalPolicy(
        dangerously_skip_all_approvals=policy.dangerously_skip_all_approvals,
        auto_approve_inherently_safe=policy.auto_approve_inherently_safe,
        auto_approve_tools=list(policy.auto_approve_tools),
    )


@dataclass(frozen=True)
class Config:
    """Per-deployment settings for AgentServiceHandler."""

    agent_task_queue: str
    workflow_name: str
    workflow_id_prefix: str
    is_message_queuing_enabled: bool


@service_handler(service=AgentServiceDefinition)
class AgentServiceHandler:
    """Exposes an agent session to external callers (e.g. the Slack connector).

    The connector calls ``sendAgentMessage`` to deliver user input and ``pollMessages`` to
    consume the agent's response stream.
    """

    def __init__(self, client: Client, config: Config) -> None:
        self._client = client
        self._config = config

    def _workflow_id(self, session_id: str) -> str:
        return self._config.workflow_id_prefix + session_id

    def _agent_client(self, session_id: str) -> AgentClient:
        """Cheap to construct per-call. Every operation but pollMessages delegates to it."""
        return AgentClient(self._client, self._workflow_id(session_id))

    # -----------------------------------------------------------------------
    # sendAgentMessage — AgentClient.start_and_submit_message()'s guess-and-retry caller
    # -----------------------------------------------------------------------

    @sync_operation
    async def send_agent_message(
        self, ctx: StartOperationContext, input: SendAgentMessageInput
    ) -> SendMessageOutput:
        try:
            payload = json.loads(input.payload)
        except json.JSONDecodeError as e:
            raise HandlerError(
                f"invalid payload JSON: {e}", type=HandlerErrorType.BAD_REQUEST
            ) from e

        start_config = AgentConfig(
            is_message_queuing_enabled=self._config.is_message_queuing_enabled
        )
        client = self._agent_client(input.session_id)

        # Nexus callers don't know expected_turn; guess 1, then re-derive from status on retry.
        expected_turn = 1
        for attempt in range(_MAX_SEND_RETRIES):
            if attempt > 0:
                status = await client.get_status()
                expected_turn = status.current_turn + len(status.pending_turns) + 1

            try:
                reply = await client.start_and_submit_message(
                    input.msg_type,
                    payload,
                    expected_turn,
                    workflow_name=self._config.workflow_name,
                    task_queue=self._config.agent_task_queue,
                    start_config=start_config,
                    update_id=f"send-{ctx.request_id}-{attempt}",
                )
            except StaleTurnError:
                await asyncio.sleep((attempt + 1) * 0.05)
                continue
            return SendMessageOutput(
                turn_number=reply.turn_number,
                turn_id=reply.turn_id,
                stream_head_offset=reply.accepted_offset,
                pending=reply.pending,
            )
        raise HandlerError(
            "send_agent_message: exhausted retries", type=HandlerErrorType.INTERNAL
        )

    # -----------------------------------------------------------------------
    # executeOperatorCommand — harness-level operator commands (no turn)
    # -----------------------------------------------------------------------

    @sync_operation
    async def execute_operator_command(
        self, ctx: StartOperationContext, input: ExecuteOperatorCommandInput
    ) -> ExecuteOperatorCommandOutput:
        result = await self._agent_client(input.session_id).execute_operator_command(
            input.name, arg=input.arg, update_id=f"op-{ctx.request_id}"
        )
        return ExecuteOperatorCommandOutput(reply=result.text)

    # -----------------------------------------------------------------------
    # approveToolCall — resolve a pending tool-approval gate
    # -----------------------------------------------------------------------

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
            raise HandlerError(str(e), type=HandlerErrorType.BAD_REQUEST) from e  # caller's fault
        return ApproveToolCallOutput(tool_id=result.tool_id, accepted=result.accepted)

    # -----------------------------------------------------------------------
    # queryOperatorInterface — discover available slash commands
    # -----------------------------------------------------------------------

    @sync_operation
    async def query_operator_interface(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> QueryOperatorInterfaceOutput:
        try:
            commands = await self._agent_client(input.session_id).get_operator_interface()
        except RPCError as e:
            if not _is_workflow_not_found(e):
                raise
            # No session started yet (no message sent) - nothing to report, not an error.
            return QueryOperatorInterfaceOutput(commands=[])
        return QueryOperatorInterfaceOutput(
            commands=[_nexus_operator_command(cmd) for cmd in commands]
        )

    # -----------------------------------------------------------------------
    # queryAgentInterface — discover @agent.accepts handlers
    # -----------------------------------------------------------------------

    @sync_operation
    async def query_agent_interface(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> AgentInterfaceOutput:
        try:
            functions = await self._agent_client(input.session_id).get_agent_interface()
        except RPCError as e:
            if not _is_workflow_not_found(e):
                raise
            # No session started yet (no message sent) - nothing to report, not an error.
            return AgentInterfaceOutput(handlers=[])
        return AgentInterfaceOutput(
            handlers=[
                NexusAcceptedFunction(
                    name=fn.name,
                    description=fn.description,
                    parameters=json.dumps(fn.parameters),
                    output=json.dumps(fn.output),
                )
                for fn in functions
            ]
        )

    # -----------------------------------------------------------------------
    # queryAgentStatus — session state snapshot
    # -----------------------------------------------------------------------

    @sync_operation
    async def query_agent_status(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> AgentStatusOutput:
        try:
            status = await self._agent_client(input.session_id).get_status()
        except RPCError as e:
            if not _is_workflow_not_found(e):
                raise
            # No session started yet (no message sent) - idle, empty status, not an error.
            return AgentStatusOutput(
                agent_id="",
                current_turn=0,
                turn_active=False,
                is_message_queuing_enabled=self._config.is_message_queuing_enabled,
                pending_turns=[],
                pending_approvals=[],
                pending_callbacks=[],
                subagents=[],
                approval_policy=NexusApprovalPolicy(
                    dangerously_skip_all_approvals=False,
                    auto_approve_inherently_safe=False,
                    auto_approve_tools=[],
                ),
                has_custom_approval_fallback=False,
            )
        return AgentStatusOutput(
            agent_id=status.agent_id,
            current_turn=status.current_turn,
            turn_active=status.turn_active,
            is_message_queuing_enabled=status.is_message_queuing_enabled,
            pending_turns=[_nexus_pending_turn(pt) for pt in status.pending_turns],
            pending_approvals=[
                NexusPendingApproval(
                    tool_id=pa.tool_id,
                    tool_name=pa.tool_name,
                    tool_input=json.dumps(pa.tool_input),
                    turn_number=pa.turn_number,
                )
                for pa in status.pending_approvals
            ],
            pending_callbacks=[
                _nexus_pending_callback(pc) for pc in status.pending_callbacks
            ],
            subagents=[_nexus_subagent_info(s) for s in status.subagents],
            approval_policy=_nexus_approval_policy(status.approval_policy),
            has_custom_approval_fallback=status.has_custom_approval_fallback,
        )

    # -----------------------------------------------------------------------
    # provideCallbackResult — fulfill a pending callback tool call
    # -----------------------------------------------------------------------

    @sync_operation
    async def provide_callback_result(
        self, ctx: StartOperationContext, input: ProvideCallbackResultInput
    ) -> ProvideCallbackResultOutput:
        try:
            result = await self._agent_client(input.session_id).provide_callback_result(
                input.tool_id,
                result=input.result,
                error=input.error,
                update_id=f"callback-{ctx.request_id}",
            )
        except CallbackResultError as e:
            raise HandlerError(str(e), type=HandlerErrorType.BAD_REQUEST) from e  # caller's fault
        return ProvideCallbackResultOutput(
            tool_id=result.tool_id, accepted=result.accepted
        )

    # -----------------------------------------------------------------------
    # closeSession — stop the agent workflow's turn loop
    # -----------------------------------------------------------------------

    @sync_operation
    async def close_session(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> CloseSessionOutput:
        handle = self._client.get_workflow_handle(self._workflow_id(input.session_id))
        try:
            await handle.signal("close")
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
            # Already gone - closing a session that doesn't exist is not an error.
        return CloseSessionOutput(session_id=input.session_id)

    # -----------------------------------------------------------------------
    # describeSession — workflow execution status, independent of app state
    # -----------------------------------------------------------------------

    @sync_operation
    async def describe_session(
        self, ctx: StartOperationContext, input: QuerySessionInput
    ) -> DescribeSessionOutput:
        workflow_id = self._workflow_id(input.session_id)
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            desc = await handle.describe()
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
            return DescribeSessionOutput(
                workflow_id=workflow_id, execution_status="NOT_FOUND", closed=True
            )
        return DescribeSessionOutput(
            workflow_id=workflow_id,
            execution_status=desc.status.name,
            closed=desc.status != WorkflowExecutionStatus.RUNNING,
        )

    # -----------------------------------------------------------------------
    # discoverSessions — running workflows of this handler's own agent type
    # -----------------------------------------------------------------------

    @sync_operation
    async def discover_sessions(
        self, ctx: StartOperationContext, input: EmptyInput
    ) -> DiscoverSessionsOutput:
        escaped_type = self._config.workflow_name.replace("'", "''")
        query = f"ExecutionStatus='Running' AND WorkflowType='{escaped_type}'"

        sessions: list[NexusSessionSummary] = []
        async for execution in self._client.list_workflows(
            query=query, limit=_DISCOVERY_LIMIT
        ):
            session_id = execution.id.removeprefix(self._config.workflow_id_prefix)
            sessions.append(
                NexusSessionSummary(
                    session_id=session_id,
                    created_at=execution.start_time.timestamp(),
                    execution_status=WorkflowExecutionStatus.RUNNING.name,
                    closed=False,
                )
            )
        return DiscoverSessionsOutput(sessions=sessions)

    # -----------------------------------------------------------------------
    # pollMessages — async operation backed by WorkflowStream's poll update
    # -----------------------------------------------------------------------

    @nexus.temporal_operation
    async def poll_messages(
        self,
        ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: PollMessagesInput,
    ) -> nexus.TemporalOperationResult[PollMessagesOutput]:
        """Long-polls WorkflowStream via update-with-callback. Returns closed=True
        synchronously if the target workflow has already completed."""
        workflow_id = self._workflow_id(input.session_id)
        timeout_seconds = input.timeout_seconds or DEFAULT_POLL_TIMEOUT_SECONDS

        try:
            result = await client.start_workflow_update(
                workflow_id,
                _WORKFLOW_STREAM_POLL_UPDATE,
                PollInput(from_offset=input.cursor, topics=[TURN_EVENTS_TOPIC]),
                result_type=PollResult,
            )
        except RPCError as e:
            if _is_workflow_already_completed(e):
                return nexus.TemporalOperationResult.sync(
                    PollMessagesOutput(
                        items=[], more_ready=False, next_offset=input.cursor, closed=True
                    )
                )
            if _is_workflow_not_found(e):
                # No message sent yet - not started, not closed, just nothing to report.
                # The caller (e.g. AttachWorkflow) should keep polling, not tear down.
                # Sleep out the same timeout a real long-poll would take - this fails
                # immediately (no workflow to attach an update-with-callback to), and
                # without the sleep a tight retry loop would hammer this operation until
                # the session's first message finally starts the workflow.
                await asyncio.sleep(timeout_seconds)
                return nexus.TemporalOperationResult.sync(
                    PollMessagesOutput(
                        items=[], more_ready=False, next_offset=input.cursor, closed=False
                    )
                )
            raise

        if result.token is not None:
            return nexus.TemporalOperationResult.async_token(result.token)

        poll_result: PollResult = result.value
        return nexus.TemporalOperationResult.sync(
            PollMessagesOutput(
                items=[
                    StreamItem(topic=item.topic, data=item.data, offset=item.offset)
                    for item in poll_result.items
                ],
                more_ready=poll_result.more_ready,
                next_offset=poll_result.next_offset,
                closed=False,
            )
        )
