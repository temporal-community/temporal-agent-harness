"""Harness-specific Nexus controls that intentionally sit outside A2A."""

from __future__ import annotations

import json
from dataclasses import dataclass

from nexusrpc import HandlerError, HandlerErrorType
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation
from temporalio.client import Client
from temporalio.service import RPCError

from temporal_agent_harness.a2a.control import HarnessControlService
from temporal_agent_harness.harness.agent_client import (
    AgentClient,
    CallbackResultError,
    ToolApprovalError,
)
from temporal_agent_harness.harness.agent_protocol import (
    OperatorCommand,
    PendingCallback,
    PendingTurn,
    SubagentInfo,
    ToolApprovalPolicy,
)

# Aliased with a Nexus* prefix where the name collides with an agent_protocol type of the
# same name but a different (reshaped, wire-friendly) shape.
from .generated import (
    AgentStatusOutput,
    ApproveToolCallInput,
    ApproveToolCallOutput,
    ExecuteOperatorCommandInput,
    ExecuteOperatorCommandOutput,
    ProvideCallbackResultInput,
    ProvideCallbackResultOutput,
    QueryOperatorInterfaceOutput,
    QuerySessionInput,
)
from .generated import (
    ApprovalPolicy as NexusApprovalPolicy,
)
from .generated import (
    OperatorCommand as NexusOperatorCommand,
)
from .generated import (
    OperatorCommandArgument as NexusOperatorCommandArgument,
)
from .generated import (
    PendingApproval as NexusPendingApproval,
)
from .generated import (
    PendingCallback as NexusPendingCallback,
)
from .generated import (
    PendingTurn as NexusPendingTurn,
)
from .generated import (
    SubagentInfo as NexusSubagentInfo,
)


def _is_workflow_not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return "workflow not found" in message or "not found for id" in message


def _nexus_operator_command(cmd: OperatorCommand) -> NexusOperatorCommand:
    # nex-gen models reject an optional field set to None — omit the kwarg instead.
    argument_kwargs: dict[str, object] = {}
    if cmd.argument is not None:
        arg_kwargs: dict[str, object] = {
            "kind": cmd.argument.kind,
            "required": cmd.argument.required,
            "choices": list(cmd.argument.choices),
            "allow_multiple": cmd.argument.allow_multiple,
        }
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


def _nexus_pending_callback(value: PendingCallback) -> NexusPendingCallback:
    return NexusPendingCallback(
        tool_id=value.tool_id,
        tool_name=value.tool_name,
        tool_input=json.dumps(value.tool_input),
        output_schema=json.dumps(value.output_schema),
        turn_number=value.turn_number,
    )


def _nexus_pending_turn(value: PendingTurn) -> NexusPendingTurn:
    return NexusPendingTurn(
        turn_number=value.turn_number, turn_id=value.turn_id, message=value.message
    )


def _nexus_subagent_info(value: SubagentInfo) -> NexusSubagentInfo:
    return NexusSubagentInfo(
        subagent_id=value.subagent_id,
        agent_key=value.agent_key,
        workflow_id=value.workflow_id,
        next_expected_turn=value.next_expected_turn,
    )


def _nexus_approval_policy(value: ToolApprovalPolicy) -> NexusApprovalPolicy:
    return NexusApprovalPolicy(
        dangerously_skip_all_approvals=value.dangerously_skip_all_approvals,
        auto_approve_inherently_safe=value.auto_approve_inherently_safe,
        auto_approve_tools=list(value.auto_approve_tools),
    )


@dataclass(frozen=True)
class HarnessControlConfig:
    workflow_id_prefix: str = ""


@service_handler(service=HarnessControlService)
class HarnessControlServiceHandler:
    """Approval, callback, status, and operator controls for harness-aware UIs.

    These operations are intentionally not part of A2A: they expose harness-specific human
    intervention and observability without contaminating the interoperable agent protocol.
    """

    def __init__(self, client: Client, config: HarnessControlConfig) -> None:
        self._client = client
        self._config = config

    def _agent_client(self, session_id: str) -> AgentClient:
        """Cheap to construct per-call; durable state remains in the target workflow."""
        return AgentClient(self._client, self._config.workflow_id_prefix + session_id)

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
        except ToolApprovalError as exc:
            raise HandlerError(str(exc), type=HandlerErrorType.BAD_REQUEST) from exc
        return ApproveToolCallOutput(tool_id=result.tool_id, accepted=result.accepted)

    # -----------------------------------------------------------------------
    # queryOperatorInterface — discover available slash commands
    # -----------------------------------------------------------------------

    @sync_operation
    async def query_operator_interface(
        self, _ctx: StartOperationContext, input: QuerySessionInput
    ) -> QueryOperatorInterfaceOutput:
        try:
            commands = await self._agent_client(
                input.session_id
            ).get_operator_interface()
        except RPCError as exc:
            if _is_workflow_not_found(exc):
                return QueryOperatorInterfaceOutput(commands=[])
            raise
        return QueryOperatorInterfaceOutput(
            commands=[_nexus_operator_command(command) for command in commands]
        )

    # -----------------------------------------------------------------------
    # queryAgentStatus — session state snapshot
    # -----------------------------------------------------------------------

    @sync_operation
    async def query_agent_status(
        self, _ctx: StartOperationContext, input: QuerySessionInput
    ) -> AgentStatusOutput:
        status = await self._agent_client(input.session_id).get_status()
        return AgentStatusOutput(
            agent_id=status.agent_id,
            current_turn=status.current_turn,
            turn_active=status.turn_active,
            is_message_queuing_enabled=status.is_message_queuing_enabled,
            pending_turns=[_nexus_pending_turn(item) for item in status.pending_turns],
            pending_approvals=[
                NexusPendingApproval(
                    tool_id=item.tool_id,
                    tool_name=item.tool_name,
                    tool_input=json.dumps(item.tool_input),
                    turn_number=item.turn_number,
                )
                for item in status.pending_approvals
            ],
            pending_callbacks=[
                _nexus_pending_callback(item) for item in status.pending_callbacks
            ],
            subagents=[_nexus_subagent_info(item) for item in status.subagents],
            approval_policy=_nexus_approval_policy(status.approval_policy),
            has_custom_approval_fallback=status.has_custom_approval_fallback,
            subagent_close_policy=status.subagent_close_policy.value,
            subagent_reuse_policy=status.subagent_reuse_policy.value,
        )

    # -----------------------------------------------------------------------
    # provideCallbackResult — fulfill a pending callback tool call
    # -----------------------------------------------------------------------

    @sync_operation
    async def provide_callback_result(
        self, ctx: StartOperationContext, input: ProvideCallbackResultInput
    ) -> ProvideCallbackResultOutput:
        # nex-gen wraps the object-shaped result in a named type instead of a plain dict.
        callback_result = (
            input.result.additional_properties if input.result is not None else None
        )
        try:
            result = await self._agent_client(input.session_id).provide_callback_result(
                input.tool_id,
                result=callback_result,
                error=input.error,
                update_id=f"callback-{ctx.request_id}",
            )
        except CallbackResultError as exc:
            raise HandlerError(str(exc), type=HandlerErrorType.BAD_REQUEST) from exc
        return ProvideCallbackResultOutput(
            tool_id=result.tool_id, accepted=result.accepted
        )
