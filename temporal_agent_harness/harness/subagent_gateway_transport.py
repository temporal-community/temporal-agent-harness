# ABOUTME: Transport for an HTTP subagent reached through the Durable Tools Gateway.

from __future__ import annotations

import json
from datetime import timedelta
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
            StartSubagentInput,
            StopSubagentInput,
        )
except ModuleNotFoundError as exc:
    raise RuntimeError(_INSTALL_MESSAGE) from exc


class GatewayTransport:
    """Reach a registered HTTP subagent through the Durable Tools Gateway.

    The alias identifies a provider. Each ``start`` call creates an instance.
    """

    def __init__(
        self,
        account_id: str,
        alias: str,
        gateway_name: str = "RegistryService",
        gateway_endpoint: str = "mcp-registry-endpoint",
    ) -> None:
        self._account_id = account_id
        self._alias = alias
        self._gateway_name = gateway_name
        self._gateway_endpoint = gateway_endpoint

    def _client(self) -> workflow.NexusClient[Any]:
        return workflow.create_nexus_client(
            service=self._gateway_name, endpoint=self._gateway_endpoint
        )

    async def start(self, *, agent_key: str, config: AgentConfig) -> str:
        out = await self._client().execute_operation(
            RegistryService.start_subagent,
            StartSubagentInput(account_id=self._account_id, alias=self._alias),
            schedule_to_close_timeout=timedelta(minutes=1),
        )
        return out.instance_id

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
                account_id=self._account_id,
                instance_id=target,
                msg_type=msg_type,
                payload=json.dumps(payload),
                expected_turn=expected_turn,
            ),
            schedule_to_close_timeout=timedelta(minutes=6),
        )
        # Publish only after the gateway returns a reply.
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
            # This transport has no remote stream cursor.
            consumed_offset=from_offset,
        )

    async def stop(self, *, target: str) -> None:
        await self._client().execute_operation(
            RegistryService.stop_subagent,
            StopSubagentInput(
                account_id=self._account_id,
                instance_id=target,
            ),
            schedule_to_close_timeout=timedelta(minutes=1),
        )
