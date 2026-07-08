# ABOUTME: NexusTransport — the Nexus-brokered implementation of the harness's
# SubagentTransport ABC (temporal_agent_harness.harness.agent_protocol.SubagentTransport).
# Drives an externally-fronted agent (via nexus/agent_adapter) as a subagent purely over Nexus
# operations; no same-cluster child workflow, no activity. Needs only a KNOWN Nexus endpoint —
# nothing about the agent registry (see nexus/subagents/registry, which builds dynamic
# discovery ON TOP of this). The harness itself never imports this; an agent author who wants
# Nexus constructs one here and passes it to
# ``runner.start_subagent(..., transport=NexusTransport(endpoint))`` or
# ``agent.subagent_toolset(..., transport=NexusTransport(endpoint))`` explicitly.
#
# Explicitly subclasses SubagentTransport (an ABC, not a structural Protocol) — Python enforces
# every abstract method is implemented before this can even be instantiated. That's possible
# with zero dependency on AgentWorkflowRunner because the ABC's own methods take only
# primitives + the already-leaf-module TurnStreamContext (see its docstring) — this class needs
# neither the runner nor the harness's internal subagent bookkeeping.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    SubagentTransport,
    SubagentTurnResult,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext

from .nexus_agent_service import AgentService, ExecuteOperatorCommandInput
from .turn_driver import run_subagent_turn_over_nexus


@dataclass
class NexusTransport(SubagentTransport):
    """Drives one subagent instance fronted by a registered Nexus endpoint (see
    nexus/agent_adapter). ``start`` is a no-op — the remote agent_adapter's
    ``sendAgentMessage`` starts (or resumes) the session lazily on the first turn
    (UpdateWithStartWorkflow); this transport only ever mints/registers a session id, never
    calls ``start_child_workflow``. ``send_turn``/``stop`` map to
    ``sendAgentMessage``/``pollMessages`` and ``executeOperatorCommand`` respectively —
    the harness's own generic ``/stop`` operator command (the same one a human ``/stop``
    reaches; no dedicated "close" Nexus operation exists or is needed)."""

    endpoint: str

    async def start(
        self,
        *,
        handle: str,
        agent_key: str,
        session_id: str,
        config: AgentConfig | None,
    ) -> None:
        del handle, agent_key, session_id, config  # unused: lazy remote start, see docstring

    async def send_turn(
        self,
        *,
        session_id: str,
        handle: str,
        agent_key: str,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
        last_consumed_offset: int,
        stream_context: TurnStreamContext | None,
        on_sent: Callable[[int], None],
    ) -> SubagentTurnResult:
        # expected_turn: the Nexus wire contract has no expected-turn staleness check today.
        # stream_context: unused — this transport sends directly from workflow code (no
        # activity), so it can report back synchronously via on_sent instead.
        del expected_turn, handle, agent_key, stream_context
        nexus_client = workflow.create_nexus_client(service=AgentService, endpoint=self.endpoint)
        return await run_subagent_turn_over_nexus(
            nexus_client,
            session_id=session_id,
            msg_type=msg_type,
            payload=payload,
            cursor=last_consumed_offset,
            on_sent=lambda sent: on_sent(sent.turn_number),
        )

    async def stop(self, *, session_id: str) -> None:
        nexus_client = workflow.create_nexus_client(service=AgentService, endpoint=self.endpoint)
        stop_handle = await nexus_client.start_operation(
            AgentService.execute_operator_command,
            ExecuteOperatorCommandInput(sessionId=session_id, name="stop"),
        )
        await stop_handle
