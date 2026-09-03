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
            # EXPLICIT: a subagent is owned by its parent and must never outlive it. Normal
            # harness shutdown asks every registered child to close gracefully; if the parent
            # instead fails, is cancelled, or is terminated before that cleanup runs, the
            # Temporal server terminates this child. We pin TERMINATE rather than rely on the SDK
            # default so the ownership guarantee can't silently change.
            #
            # TODO: we may prefer to handle abnormal parent shutdown more gracefully than a hard
            # TERMINATE (which kills the child mid-turn with no cleanup — no `close` handling,
            # no chance to finalize in-flight work). Two candidate approaches:
            #   1. REQUEST_CANCEL — the server requests cancellation of the child on parent
            #      close, letting a child that handles cancellation tear down gracefully
            #      (requires the harness agent loop to treat cancellation as a clean stop).
            #   2. A workflow finalization/cleanup hook on the parent that, before it exits,
            #      stops every still-registered subagent through the SAME "front door" a
            #      human/UI uses — i.e. `stop_subagent` → the `close` signal — so children
            #      shut down via their normal graceful path rather than being killed by the
            #      server. The runner does this for its normal `close` path today; extending
            #      that guarantee to failure/cancellation still needs explicit handling.
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
