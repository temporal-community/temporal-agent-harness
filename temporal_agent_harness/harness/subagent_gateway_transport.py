# ABOUTME: A2A transport for an HTTP agent routed through the Durable Tools Gateway.

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from a2a.types import CancelTaskRequest, Message, Part, Role, SendMessageRequest
    from google.protobuf.json_format import MessageToDict
    from nexus_a2a import A2AService

from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    SubagentMessageSent,
    SubagentTurnResult,
)
from temporal_agent_harness.harness.agent_workflow import _current_runner
from temporal_agent_harness.harness.stream_context import TurnStreamContext

_INSTALL_MESSAGE = (
    "Gateway-brokered subagent support requires the optional `nexus-mcp` extra, which is "
    "only resolvable from an editable checkout of this repo (it path-depends on nexus/mcp) "
    "and requires Python >=3.13. Install it with `uv sync --extra nexus-mcp`."
)


class GatewayTransport:
    """Route A2A over Nexus to a registered HTTP A2A agent."""

    def __init__(
        self,
        account_id: str,
        alias: str,
        gateway_name: str = "A2AService",
        gateway_endpoint: str = "mcp-registry-endpoint",
    ) -> None:
        self._account_id = account_id
        self._alias = alias
        self._gateway_name = gateway_name
        self._gateway_endpoint = gateway_endpoint

    def _client(self) -> workflow.NexusClient[A2AService]:
        return workflow.create_nexus_client(service=A2AService, endpoint=self._gateway_endpoint)

    async def start(self, *, agent_key: str, config: AgentConfig) -> str:
        # A2A tasks are allocated locally and started lazily by their first Message.
        return f"{agent_key}-subagent-{workflow.uuid4()}"

    async def dispatch(
        self,
        *,
        target: str,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        from_offset: int,
        handle: str,
        agent_key: str,
        parent_stream_context: TurnStreamContext,
    ) -> SubagentTurnResult:
        out = await self._client().execute_operation(
            A2AService.send_message,
            SendMessageRequest(
                message=Message(
                    message_id=str(workflow.uuid4()),
                    task_id=target,
                    context_id=target,
                    role=Role.ROLE_USER,
                    parts=[Part(text=str(payload.get("text", "")))],
                    metadata={"temporal.io/message-type": msg_type},
                ),
                metadata={
                    "account_id": self._account_id,
                    "agent_id": self._alias,
                    "expected_turn": expected_turn,
                },
            ),
            schedule_to_close_timeout=timedelta(minutes=6),
        )
        metadata = MessageToDict(out.task.metadata, preserving_proto_field_name=True)
        turn_number = int(metadata["temporal.io/turn-number"])
        turn_id = str(metadata["temporal.io/turn-id"])
        text = "".join(
            part.text
            for artifact in out.task.artifacts
            for part in artifact.parts
            if part.HasField("text")
        )
        _current_runner().publish(
            SubagentMessageSent(
                subagent_id=handle,
                agent_key=agent_key,
                workflow_id=target,
                function=msg_type,
                subagent_turn=turn_number,
                from_offset=from_offset,
            )
        )
        return SubagentTurnResult(
            output={"text": text},
            turn_id=turn_id,
            turn_number=turn_number,
            consumed_offset=from_offset,
        )

    async def stop(self, *, target: str) -> None:
        await self._client().execute_operation(
            A2AService.cancel_task,
            CancelTaskRequest(
                id=target,
                metadata={"account_id": self._account_id},
            ),
            schedule_to_close_timeout=timedelta(minutes=1),
        )
