# ABOUTME: Default transport for a subagent that runs as a local child workflow.

from __future__ import annotations

from typing import Any

from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol import (
    DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
    DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
    RUN_SUBAGENT_TURN_ACTIVITY,
    AgentConfig,
    RunSubagentTurnInput,
    SubagentTurnResult,
)
from temporal_agent_harness.harness.stream_context import TurnStreamContext


class ChildWorkflowTransport:
    """Run a subagent as a child workflow in the same cluster.

    This is the original harness-native subagent path, now isolated behind the same transport
    interface as Nexus/A2A subagents.
    """

    def __init__(self, workflow_type: str, task_queue: str) -> None:
        self._workflow_type = workflow_type
        self._task_queue = task_queue

    async def start(self, *, agent_key: str, config: AgentConfig) -> str:
        workflow_id = f"{agent_key}-subagent-{workflow.uuid4()}"
        await workflow.start_child_workflow(
            self._workflow_type,
            config,
            id=workflow_id,
            task_queue=self._task_queue,
            # The runner applies its live subagent close policy during graceful shutdown.
            # ABANDON is required because Temporal cannot change a child workflow's
            # parent-close policy after it has started: ``keep-open`` and ``ask-user`` must be
            # able to leave the child alive, while ``close`` explicitly stops it through the
            # same front door used by a human/UI.
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )
        return workflow_id

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
        # The dispatch marker (SubagentMessageSent) is published by the activity itself,
        # WHEN it actually sends the message to the child — not here at execute_activity
        # dispatch time (there's a real gap before the activity runs). Mirrors how tool
        # activities publish tool_start from inside the activity. We pass the parent's turn
        # context + handle/agent_key so the activity can publish onto THIS agent's stream;
        # the activity's heartbeat memo dedupes the publish across retries (it only fires on
        # a fresh send, never on a heartbeat-resume).
        return await workflow.execute_activity(
            RUN_SUBAGENT_TURN_ACTIVITY,
            RunSubagentTurnInput(
                child_workflow_id=target,
                type=msg_type,
                payload=payload,
                expected_turn=expected_turn,
                from_offset=from_offset,
                handle=handle,
                agent_key=agent_key,
                parent_stream_context=parent_stream_context,
            ),
            start_to_close_timeout=DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
            heartbeat_timeout=DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
            result_type=SubagentTurnResult,
        )

    async def stop(self, *, target: str) -> None:
        await workflow.get_external_workflow_handle(target).signal("close")
