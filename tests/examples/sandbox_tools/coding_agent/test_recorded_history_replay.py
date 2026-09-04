# ABOUTME: Replays histories RECORDED FROM REAL SESSIONS against today's workflow code, which is
# the one thing a self-generated history cannot check: a history produced by the code under test
# is deterministic by construction, so it stays green through exactly the changes that wedge a
# live session. These files were recorded once and frozen; nothing regenerates them.
#
# The bug they exist for: session 094da980 ran for four hours, then a worker was restarted onto
# code that had just gained @agent.snapshot / @agent.restore and dropped `stream=WorkflowStream()`.
# Both of those are vetoes in AgentWorkflowRunner._rollover_blocked_reason, so flipping them turned
# continue-as-new ON for a session whose history already carried suggestContinueAsNew=true. On
# replay the runner rolled the session over at its first quiescent turn boundary and scheduled the
# sandbox teardown, where history had an update acceptance:
#
#   [TMPRL1100] Nondeterminism error: Activity machine does not handle this event:
#     HistoryEvent(id: 4172, WorkflowExecutionUpdateAccepted)
#
# Queries are served by replay, so every query against that session failed for good. The fix was
# process, not code — don't hot-swap workflow code under live runs — and this is the guard that
# says so out loud the next time a veto moves.
#
# Run with: uv run pytest tests/examples/sandbox_tools/coding_agent/test_recorded_history_replay.py

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from examples.sandbox_tools.coding_agent.worker import openai_agents_plugin
from examples.sandbox_tools.coding_agent.workflow import SandboxedCodingAgentWorkflow

HISTORIES = Path(__file__).parent / "histories"


@dataclass(frozen=True)
class Recorded:
    """A frozen history and the property that makes replaying it worth the bytes."""

    filename: str
    rolled_over: bool
    summary: str


# Payloads the workflow never reads back on replay — update outcomes, activity inputs, the
# continue-as-new blob — were blanked, the carried conversation and stream tail were replaced
# with placeholders, and `/Users/<name>` and the recording host's name were substituted. None of
# that can quietly weaken the check: anything that altered a workflow decision would stop
# matching the recorded command stream, and these tests would fail.
RECORDED = (
    Recorded(
        "rolled_over_session.json.gz",
        rolled_over=True,
        summary="a session that crossed the threshold and handed itself to a successor run",
    ),
    Recorded(
        "tool_using_session.json.gz",
        rolled_over=False,
        summary="four turns of sandbox activate/pause, a tool approval, and model calls",
    ),
)


def _load(recorded: Recorded) -> WorkflowHistory:
    with gzip.open(HISTORIES / recorded.filename, "rb") as fh:
        return WorkflowHistory.from_json(recorded.filename, json.load(fh))


@pytest.mark.parametrize("recorded", RECORDED, ids=lambda r: r.filename)
def test_a_recorded_history_still_says_what_it_was_kept_for(recorded: Recorded) -> None:
    """Guard the fixture itself: a history that lost its shape would prove less in silence."""
    events = _load(recorded).events

    rolled_over = any(
        event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW
        for event in events
    )
    assert rolled_over == recorded.rolled_over, (
        f"{recorded.filename} was kept because it is {recorded.summary}; it no longer is, "
        "so replaying it does not check what this file claims"
    )

    if recorded.rolled_over:
        # The load-bearing one. Temporal records its own suggestion on every WorkflowTaskStarted,
        # which is what makes the rollover decision replayable at all — and what makes this
        # history able to notice a veto moving underneath it.
        assert any(
            event.workflow_task_started_event_attributes.suggest_continue_as_new
            for event in events
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED
        ), "a rollover history with no recorded suggestion cannot pin the rollover decision"


@pytest.mark.parametrize("recorded", RECORDED, ids=lambda r: r.filename)
@pytest.mark.asyncio
async def test_a_recorded_session_replays_against_todays_code(recorded: Recorded) -> None:
    # No server and no worker: a Replayer over a frozen history is the whole check, and it is
    # the same check the worker performs before it will answer a query about a live session.
    await Replayer(
        workflows=[SandboxedCodingAgentWorkflow],
        plugins=[openai_agents_plugin()],
    ).replay_workflow(_load(recorded))
