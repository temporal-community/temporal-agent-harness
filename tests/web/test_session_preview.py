# ABOUTME: The session list's one-line preview. It comes off the workflow memo, which rides along
# on the describe the row already needed, and falls back to the old history scan only for sessions
# that started before agents wrote a memo. These pin which path is taken, because the whole point
# is what is NOT done: a scan that pages up to 96 history events per row to usually find nothing.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_protocol import INITIAL_USER_MESSAGE_MEMO
from temporal_agent_harness.web.app import _session_with_execution_state
from temporal_agent_harness.web.session_manager import Session

ENVELOPE = '{"type":"ask","payload":{"text":"how do I book a flight"}}'


def _session(workflow_id: str = "agent-session-1") -> Session:
    return Session(
        workflow_id=workflow_id,
        created_at=0.0,
        label="Session 1",
        agent_workflow_type="HelloAgent",
    )


class _Handle:
    def __init__(self, *, memo: dict | None = None, error: Exception | None = None) -> None:
        self._memo = memo or {}
        self._error = error

    async def describe(self):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            status=WorkflowExecutionStatus.RUNNING, memo=self._memo_async
        )

    async def _memo_async(self):
        return self._memo


class _Temporal:
    def __init__(self, handle: _Handle) -> None:
        self._handle = handle

    def get_workflow_handle(self, _workflow_id: str):
        return self._handle


class _Scan:
    """Stands in for the history scan, and counts whether anyone asked it to run."""

    def __init__(self, result: str | None = None) -> None:
        self.calls = 0
        self.result = result

    async def __call__(self, _temporal, _workflow_id):
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_the_preview_comes_off_the_memo_without_touching_history() -> None:
    scan = _Scan("should never be used")
    temporal = _Temporal(_Handle(memo={INITIAL_USER_MESSAGE_MEMO: ENVELOPE}))

    with patch("temporal_agent_harness.web.app._session_initial_user_message", scan):
        row = await _session_with_execution_state(temporal, _session())

    assert row["initial_user_message"] == "how do I book a flight"
    assert scan.calls == 0


@pytest.mark.asyncio
async def test_a_session_older_than_the_memo_still_gets_its_preview() -> None:
    # Falling back rather than blanking: every session that exists today predates the memo, and
    # the cost drains away on its own as sessions turn over.
    scan = _Scan("what it said before memos existed")
    temporal = _Temporal(_Handle(memo={}))

    with patch("temporal_agent_harness.web.app._session_initial_user_message", scan):
        row = await _session_with_execution_state(temporal, _session())

    assert row["initial_user_message"] == "what it said before memos existed"
    assert scan.calls == 1


@pytest.mark.asyncio
async def test_a_workflow_that_is_gone_is_not_scanned_for_a_preview() -> None:
    # The measured waste: 39 of 40 rows walked up to 96 events each to return nothing, because
    # a workflow retention has deleted has no history left to scan.
    scan = _Scan("unreachable")
    temporal = _Temporal(
        _Handle(error=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    )

    with patch("temporal_agent_harness.web.app._session_initial_user_message", scan):
        row = await _session_with_execution_state(temporal, _session())

    assert row["execution_status"] == "NOT_FOUND"
    assert "initial_user_message" not in row
    assert scan.calls == 0


@pytest.mark.asyncio
async def test_a_session_nobody_has_spoken_to_reports_no_preview() -> None:
    scan = _Scan(None)
    temporal = _Temporal(_Handle(memo={}))

    with patch("temporal_agent_harness.web.app._session_initial_user_message", scan):
        row = await _session_with_execution_state(temporal, _session())

    assert "initial_user_message" not in row


@pytest.mark.asyncio
async def test_an_unreadable_memo_costs_the_preview_and_nothing_else() -> None:
    handle = _Handle(memo={})

    async def _boom():
        raise RuntimeError("memo decode failed")

    handle._memo_async = _boom
    scan = _Scan(None)

    with patch("temporal_agent_harness.web.app._session_initial_user_message", scan):
        row = await _session_with_execution_state(_Temporal(handle), _session())

    assert row["execution_status"] == "RUNNING"
