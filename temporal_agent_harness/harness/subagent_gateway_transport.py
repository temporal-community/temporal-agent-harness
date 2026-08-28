# ABOUTME: GatewayTransport -- SubagentTransport for a non-Nexus subagent, brokered through
# the Durable Tools Gateway (the same deployable that brokers 3rd-party MCP servers).
# Requires the `nexus-mcp` extra.

from __future__ import annotations

import json
from typing import Any

from temporalio import workflow

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

try:
    with workflow.unsafe.imports_passed_through():
        from durable_tools_gateway.generated import (
            DispatchSubagentTurnInput,
            RegistryService,
            StopSubagentInput,
        )
except ModuleNotFoundError as exc:
    raise RuntimeError(_INSTALL_MESSAGE) from exc


class GatewayTransport:
    """A non-Nexus subagent, brokered through the Durable Tools Gateway.

    `agent_id` / `alias` identify the registration (see
    RegistryServiceHandler.register_subagent) - there is no separate "instance" concept, the
    registered alias IS the instance. `start` does not create anything remotely.
    """

    def __init__(
        self,
        agent_id: str,
        alias: str,
        gateway_name: str = "RegistryService",
        gateway_endpoint: str = "mcp-registry-endpoint",
    ) -> None:
        self._agent_id = agent_id
        self._alias = alias
        self._gateway_name = gateway_name
        self._gateway_endpoint = gateway_endpoint

    def _client(self) -> workflow.NexusClient[Any]:
        return workflow.create_nexus_client(
            service=self._gateway_name, endpoint=self._gateway_endpoint
        )

    async def start(self, *, agent_key: str, config: AgentConfig) -> str:
        return self._alias

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
            RegistryService.dispatch_subagent_turn,
            DispatchSubagentTurnInput(
                agent_id=self._agent_id,
                alias=target,
                msg_type=msg_type,
                payload=json.dumps(payload),
                expected_turn=expected_turn,
            ),
        )
        # No activity backs this call on the caller side either - publish the dispatch
        # marker here, right after the send succeeds (see NexusTransport for the same
        # reasoning). A failed dispatch_subagent_turn call raises before this line, so no
        # marker is published for a rejected send.
        _current_runner().publish(
            SubagentMessageSent(
                subagent_id=handle,
                agent_key=agent_key,
                workflow_id=target,
                function=msg_type,
                subagent_turn=out.turn_number,
                from_offset=from_offset,
            )
        )
        return SubagentTurnResult(
            output=json.loads(out.output),
            turn_id=out.turn_id,
            turn_number=out.turn_number,
            # No remote stream to resume from for this transport - unchanged, vestigial.
            consumed_offset=from_offset,
        )

    async def stop(self, *, target: str) -> None:
        await self._client().execute_operation(
            RegistryService.stop_subagent,
            StopSubagentInput(agent_id=self._agent_id, alias=target),
        )
