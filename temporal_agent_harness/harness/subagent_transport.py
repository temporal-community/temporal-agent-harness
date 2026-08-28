# ABOUTME: ChildWorkflowTransport -- the default SubagentTransport. Drives a subagent as a
# same-cluster Temporal child workflow, via the existing RUN_SUBAGENT_TURN_ACTIVITY activity.
# Extracted from AgentWorkflowRunner.start_subagent/run_subagent_turn/stop_subagent - same
# behavior, now behind the SubagentTransport seam so other transports (NexusTransport,
# GatewayTransport) can sit next to it.

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
    """Same-cluster child workflow. `workflow_type` / `task_queue` name the child to start.

    Sends the SubagentMessageSent marker itself, from inside RUN_SUBAGENT_TURN_ACTIVITY, at
    the moment it actually sends (see subagent_activities.py) - dispatch() does not publish
    it.
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
            # EXPLICIT: a subagent is owned by its parent and must never outlive it. If the
            # parent closes for ANY reason before stop() was called, the Temporal server
            # terminates this child. Pinned rather than relying on the SDK default so the
            # guarantee can't silently change.
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
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
