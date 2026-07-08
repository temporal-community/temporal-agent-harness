# ABOUTME: ChildWorkflowTransport — the harness's built-in SubagentTransport implementation
# (see temporal_agent_harness.harness.agent_protocol.SubagentTransport): a same-cluster
# Temporal child workflow, driven via the submit/consume activities in subagent_activities.py.
# Activities, specifically, because workflow code can't hold a Client — an update-plus-
# stream-read needs one — unlike a Nexus operation, which a workflow CAN await directly (see
# nexus/subagents' NexusTransport, which needs no activity at all).
#
# This module has NO dependency on agent_workflow.py, in either direction: SubagentTransport's
# methods take only primitives (see its own docstring for why), and AgentWorkflowRunner is the
# one importing THIS module, not the reverse.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.agent_protocol import (
    CONSUME_SUBAGENT_TURN_ACTIVITY,
    DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
    DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
    DEFAULT_SUBAGENT_SUBMIT_START_TO_CLOSE_TIMEOUT,
    SUBMIT_SUBAGENT_TURN_ACTIVITY,
    AgentConfig,
    ConsumeSubagentTurnInput,
    SubagentTransport,
    SubagentTurnResult,
    SubmitSubagentTurnInput,
    SubmitSubagentTurnResult,
)


@dataclass
class ChildWorkflowTransport(SubagentTransport):
    """Drives one subagent instance as a same-cluster Temporal child workflow. The exact
    behavior ``start_subagent``/``run_subagent_turn``/``stop_subagent`` always had — just
    factored out into a :class:`SubagentTransport` implementation.

    ``_pending_turn_id`` is set by ``dispatch`` and consumed by the very next ``await_reply``
    call — safe because the runner's per-subagent FIFO gate guarantees one instance never has
    two turns in flight at once (see ``SubagentTransport.await_reply``'s docstring)."""

    workflow_type: str
    task_queue: str
    _pending_turn_id: str | None = field(default=None, init=False, repr=False)

    async def start(
        self,
        *,
        handle: str,
        agent_key: str,
        session_id: str,
        config: AgentConfig | None,
    ) -> None:
        del agent_key  # unused: the child's own agent_id comes from `handle`, not agent_key
        # Push the handle down as the child's own agent_id so the child stamps it on every
        # event it publishes — unifying "the id the parent references this subagent by" with
        # "the id on the subagent's own stream", which is what lets a client merge the two
        # streams coherently (and, since the handle is tree-unique, group by agent_id without
        # collisions). This is the one config field the parent overrides per-child; a
        # caller-supplied agent_id would not match the parent's handle.
        child_config = (config if config is not None else AgentConfig()).model_copy(
            update={"agent_id": handle}
        )
        await workflow.start_child_workflow(
            self.workflow_type,
            child_config,
            id=session_id,
            task_queue=self.task_queue,
            # EXPLICIT: a subagent is owned by its parent and must never outlive it. If the
            # parent closes for ANY reason (its own `close` signal, completion, failure,
            # cancellation, or termination) before `stop_subagent` was called, the Temporal
            # server terminates this child. We pin TERMINATE rather than rely on the SDK
            # default so the guarantee can't silently change. (Graceful shutdown of a still-
            # wanted subagent is the explicit `stop_subagent` path, which sends `close`.)
            #
            # TODO: we may prefer to handle parent shutdown more gracefully than a hard
            # TERMINATE (which kills the child mid-turn with no cleanup — no `close` handling,
            # no chance to finalize in-flight work). Two candidate approaches:
            #   1. REQUEST_CANCEL — the server requests cancellation of the child on parent
            #      close, letting a child that handles cancellation tear down gracefully
            #      (requires the harness agent loop to treat cancellation as a clean stop).
            #   2. A workflow finalization/cleanup hook on the parent that, before it exits,
            #      stops every still-registered subagent through the SAME "front door" a
            #      human/UI uses — i.e. `stop_subagent` → the `close` signal — so children
            #      shut down via their normal graceful path rather than being killed by the
            #      server. (This keeps shutdown semantics uniform with manual stops, but must
            #      run on every parent-exit path, including failure/cancellation.)
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
        )

    async def dispatch(
        self,
        *,
        session_id: str,
        msg_type: str,
        payload: dict[str, Any],
        expected_turn: int,
    ) -> int:
        result: SubmitSubagentTurnResult = await workflow.execute_activity(
            SUBMIT_SUBAGENT_TURN_ACTIVITY,
            SubmitSubagentTurnInput(
                child_workflow_id=session_id,
                type=msg_type,
                payload=payload,
                expected_turn=expected_turn,
            ),
            start_to_close_timeout=DEFAULT_SUBAGENT_SUBMIT_START_TO_CLOSE_TIMEOUT,
            result_type=SubmitSubagentTurnResult,
        )
        self._pending_turn_id = result.turn_id
        return result.turn_number

    async def await_reply(
        self,
        *,
        session_id: str,
        turn_number: int,
        last_consumed_offset: int,
    ) -> SubagentTurnResult:
        turn_id = self._pending_turn_id
        if turn_id is None:
            raise ApplicationError(
                "await_reply called with no dispatched turn pending",
                type="NoDispatchedTurn",
                non_retryable=True,
            )
        return await workflow.execute_activity(
            CONSUME_SUBAGENT_TURN_ACTIVITY,
            ConsumeSubagentTurnInput(
                child_workflow_id=session_id,
                turn_id=turn_id,
                turn_number=turn_number,
                from_offset=last_consumed_offset,
            ),
            start_to_close_timeout=DEFAULT_SUBAGENT_START_TO_CLOSE_TIMEOUT,
            heartbeat_timeout=DEFAULT_SUBAGENT_HEARTBEAT_TIMEOUT,
            result_type=SubagentTurnResult,
        )

    async def stop(self, *, session_id: str) -> None:
        await workflow.get_external_workflow_handle(session_id).signal("close")
