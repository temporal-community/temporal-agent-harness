"""Harness-specific controls intentionally outside the A2A agent protocol."""

from __future__ import annotations

import nexusrpc

from temporal_agent_harness.nexus_agent_adapter.generated import (
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


@nexusrpc.service(name="HarnessControlService")
class HarnessControlService:
    """Approval, callback, status, and operator controls for harness-aware UIs."""

    execute_operator_command = nexusrpc.Operation(
        "ExecuteOperatorCommand",
        ExecuteOperatorCommandInput,
        ExecuteOperatorCommandOutput,
    )
    approve_tool_call = nexusrpc.Operation(
        "ApproveToolCall", ApproveToolCallInput, ApproveToolCallOutput
    )
    query_operator_interface = nexusrpc.Operation(
        "QueryOperatorInterface", QuerySessionInput, QueryOperatorInterfaceOutput
    )
    query_agent_status = nexusrpc.Operation(
        "QueryAgentStatus", QuerySessionInput, AgentStatusOutput
    )
    provide_callback_result = nexusrpc.Operation(
        "ProvideCallbackResult",
        ProvideCallbackResultInput,
        ProvideCallbackResultOutput,
    )
