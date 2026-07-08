# ABOUTME: NexusTransport — drives a subagent purely over Nexus (subagent_adapter), no
# same-cluster child workflow, no activity. Internally A2A-shaped (Task/Message) but implements
# the same SubagentTransport ABC as ChildWorkflowTransport, so the runner/FIFO gate/stream merge
# need zero changes. session_id (ABC) and task_id (A2A) are the same value, different name.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    SubagentTransport,
    SubagentTurnResult,
)

from .nexus_agent_service import SubagentService
from .turn_driver import (
    cancel_task_over_nexus,
    poll_task_updates_over_nexus,
    send_message_over_nexus,
)


@dataclass
class NexusTransport(SubagentTransport):
    """Drives one subagent instance fronted by a registered Nexus endpoint.

    ``start`` is a no-op — sendMessage starts/resumes the task lazily via UpdateWithStartWorkflow.
    ``stop`` maps to cancelTask (the harness's generic /stop command).

    ``_pending`` is set by ``dispatch`` and consumed by the next ``await_reply`` — safe because
    the FIFO gate guarantees one instance never has two turns in flight."""

    endpoint: str
    _pending: tuple[str, int] | None = field(default=None, init=False, repr=False)

    async def start(
        self,
        *,
        handle: str,
        agent_key: str,
        session_id: str,
        config: AgentConfig | None,
    ) -> None:
        del handle, agent_key, session_id, config  # lazy remote start, see docstring

    async def dispatch(
        self,
        *,
        session_id: str,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
    ) -> int:
        del expected_turn  # no expected-turn staleness check on the Nexus wire today
        nexus_client = workflow.create_nexus_client(service=SubagentService, endpoint=self.endpoint)
        task = await send_message_over_nexus(
            nexus_client, task_id=session_id, handler=msg_type, payload=payload
        )
        # message_id (await_reply's poll filter) + stream_head_offset (default poll cursor).
        message_id = task.status.message.message_id if task.status.message else ""
        self._pending = (message_id, task.stream_head_offset)
        return task.turn_number

    async def await_reply(
        self,
        *,
        session_id: str,
        turn_number: int,
        last_consumed_offset: int,
    ) -> SubagentTurnResult:
        if self._pending is None:
            raise ApplicationError(
                "await_reply called with no dispatched turn pending",
                type="NoDispatchedTurn",
                non_retryable=True,
            )
        message_id, stream_head_offset = self._pending
        nexus_client = workflow.create_nexus_client(service=SubagentService, endpoint=self.endpoint)
        return await poll_task_updates_over_nexus(
            nexus_client,
            task_id=session_id,
            message_id=message_id,
            turn_number=turn_number,
            cursor=last_consumed_offset or stream_head_offset,
        )

    async def stop(self, *, session_id: str) -> None:
        nexus_client = workflow.create_nexus_client(service=SubagentService, endpoint=self.endpoint)
        await cancel_task_over_nexus(nexus_client, task_id=session_id)

    def nexus_endpoint(self) -> str | None:
        """Overrides the default — this transport's stream is read via Nexus poll, not
        WorkflowStreamClient (see stream_source.py)."""
        return self.endpoint
