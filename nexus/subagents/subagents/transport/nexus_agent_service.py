# ABOUTME: Sandbox-safe Python mirror of the Go agent_adapter's AgentService Nexus contract
# (nexus/agent_adapter/nexus_worker/agent.nexusrpc.yaml). Lets a parent agent's workflow code
# call ``workflow.create_nexus_client(service=AgentService, endpoint=...)`` to drive a
# Nexus-fronted agent as a subagent. Field names/casing mirror the Go wire types EXACTLY
# (including their few snake_case outliers, e.g. ``next_offset``) since this crosses the wire
# as plain JSON and must match byte-for-byte what the Go handler expects/produces — this is
# NOT a stylistic choice, it is the wire contract. Sandbox-safe (stdlib + pydantic + nexusrpc
# only) so it imports cleanly inside the Temporal workflow sandbox.

from __future__ import annotations

import nexusrpc
from pydantic import BaseModel, ConfigDict, Field


class SendAgentMessageInput(BaseModel):
    """Mirrors the Go handler's ``SendAgentMessageInput`` (generated.go)."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    msg_type: str = Field(alias="msgType")
    payload: str


class SendMessageOutput(BaseModel):
    """Mirrors the Go handler's ``SendMessageOutput``. Note the output type keeps the
    (unrenamed) ``SendMessage`` prefix on the Go side even though the operation itself is
    ``sendAgentMessage`` — an asymmetry in the Go codegen, mirrored here deliberately."""

    model_config = ConfigDict(populate_by_name=True)

    turn_number: int = Field(alias="turnNumber")
    turn_id: str = Field(alias="turnId")
    stream_head_offset: int = Field(default=0, alias="streamHeadOffset")
    pending: bool = False


class PollMessagesInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    cursor: int
    timeout_seconds: float | None = Field(default=None, alias="timeoutSeconds")


class StreamItem(BaseModel):
    """One raw item from ``PollMessagesOutput.items`` — ``data`` is
    base64(proto Payload{encoding:json/plain, data:AgentEvent JSON}), matching exactly what
    ``WorkflowStream._log`` stores (see ``StreamItem``'s docstring)."""

    topic: str
    data: str
    offset: int


class PollMessagesOutput(BaseModel):
    """Mirrors the Go handler's ``PollMessagesOutput``. ``next_offset``/``more_ready`` are
    genuinely snake_case on the Go wire too (an inconsistency in the source IDL, not a typo
    here) so they need no alias."""

    items: list[StreamItem] = Field(default_factory=list)
    next_offset: int = 0
    more_ready: bool = False
    closed: bool = False


class ExecuteOperatorCommandInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    name: str
    arg: str = ""


class ExecuteOperatorCommandOutput(BaseModel):
    reply: str


@nexusrpc.service
class AgentService:
    """Python-side reference to the Go ``AgentService`` Nexus service
    (nexus/agent_adapter/nexus_worker). Only the operations the Nexus-brokered subagent path
    needs are declared here — ``approveToolCall``/the query operations aren't used by this
    path and are intentionally omitted rather than kept in lockstep for no reason."""

    send_agent_message: nexusrpc.Operation[SendAgentMessageInput, SendMessageOutput] = (
        nexusrpc.Operation(
            name="sendAgentMessage",
            input_type=SendAgentMessageInput,
            output_type=SendMessageOutput,
        )
    )
    poll_messages: nexusrpc.Operation[PollMessagesInput, PollMessagesOutput] = (
        nexusrpc.Operation(
            name="pollMessages",
            input_type=PollMessagesInput,
            output_type=PollMessagesOutput,
        )
    )
    execute_operator_command: nexusrpc.Operation[
        ExecuteOperatorCommandInput, ExecuteOperatorCommandOutput
    ] = nexusrpc.Operation(
        name="executeOperatorCommand",
        input_type=ExecuteOperatorCommandInput,
        output_type=ExecuteOperatorCommandOutput,
    )
