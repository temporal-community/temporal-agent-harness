# ABOUTME: /api/attach must never end in silence. The response headers are already out by the time
# the body runs, so a failure has no status code left to travel on — it has to be an in-band
# ``error`` frame, and the one case with no exception behind it (a closed run whose stream cannot
# be replayed) has to be reported too, or a dead session is indistinguishable from a caught-up one.

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app

SESSION = "agent-session-dead"


@dataclass
class _Description:
    status: WorkflowExecutionStatus


class _Handle:
    def __init__(self, status: WorkflowExecutionStatus | None) -> None:
        self._status = status

    async def describe(self) -> _Description:
        if self._status is None:
            raise RPCError("gone", RPCStatusCode.NOT_FOUND, b"")
        return _Description(self._status)


class _Temporal:
    def __init__(self, status: WorkflowExecutionStatus | None) -> None:
        self._status = status

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        return _Handle(self._status)


class _Stream:
    def __init__(self, offset: int) -> None:
        self._offset = offset

    async def get_offset(self) -> int:
        return self._offset


def _agent_client(*, raises: Exception | None = None, frames: list[bytes] | None = None):
    """Stand in for AgentClient, whose ``attach`` either fails or yields rendered frames."""

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def attach(self, *, on_item, from_offset):
            if raises is not None:
                raise raises

            async def _iter():
                for frame in frames or []:
                    yield frame

            return _iter()

    return _Client


@contextmanager
def _app(
    *,
    status: WorkflowExecutionStatus | None,
    published: int = 0,
    raises: Exception | None = None,
    frames: list[bytes] | None = None,
):
    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.temporal = _Temporal(status)
    with (
        patch(
            "temporal_agent_harness.web.app.AgentClient",
            _agent_client(raises=raises, frames=frames),
        ),
        patch(
            "temporal_agent_harness.web.app.WorkflowStreamClient.create",
            lambda *_args: _Stream(published),
        ),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def _frames(body: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _attach(client: TestClient, from_offset: int = 0):
    return client.get(f"/api/attach?session_id={SESSION}&from_offset={from_offset}")


def test_workflow_gone_is_reported_in_band() -> None:
    gone = RPCError(f"workflow not found for ID: {SESSION}", RPCStatusCode.NOT_FOUND, b"")
    with _app(status=None, raises=gone) as client:
        response = _attach(client)

    assert response.status_code == 200
    (frame,) = _frames(response.text)
    assert frame["code"] == "workflow_not_found"
    assert frame["kind"] == "unavailable"
    # No ``type``: that is how the console tells a fact about the connection apart from an
    # event of the run, and how it decides to raise connectionError.
    assert "type" not in frame


def test_other_rpc_failures_are_reported_too_and_named() -> None:
    denied = RPCError("permission denied", RPCStatusCode.PERMISSION_DENIED, b"")
    with _app(status=None, raises=denied) as client:
        response = _attach(client)

    (frame,) = _frames(response.text)
    assert frame["code"] == "stream_unavailable"
    assert "PERMISSION_DENIED" in frame["message"]


def test_a_throttle_does_not_send_the_reader_after_reachability() -> None:
    """RESOURCE_EXHAUSTED is Temporal answering. The old message said to check that it
    was reachable, which is the one thing the status has already proved."""
    throttled = RPCError("busy workflow", RPCStatusCode.RESOURCE_EXHAUSTED, b"")
    with _app(status=None, raises=throttled) as client:
        response = _attach(client)

    (frame,) = _frames(response.text)
    assert frame["code"] == "stream_unavailable"
    assert "RESOURCE_EXHAUSTED" in frame["message"]
    assert "reachable and answered" in frame["message"]
    assert "Check that Temporal is reachable" not in frame["message"]


@pytest.mark.parametrize(
    "status",
    [
        RPCStatusCode.UNAVAILABLE,
        RPCStatusCode.DEADLINE_EXCEEDED,
        RPCStatusCode.CANCELLED,
    ],
)
def test_an_outage_still_names_the_worker_and_the_stack(status: RPCStatusCode) -> None:
    """The advice the throttle lost is correct here, and has to survive."""
    with _app(status=None, raises=RPCError(status.name, status, b"")) as client:
        response = _attach(client)

    (frame,) = _frames(response.text)
    assert status.name in frame["message"]
    assert "worker is polling this agent's task queue" in frame["message"]


def test_a_query_the_worker_refused_is_reported_too() -> None:
    # Not an RPCError, and the ordinary way a dev stack fails: the worker replays a history
    # written by an earlier build of the agent to answer the status query, and cannot.
    refused = WorkflowQueryFailedError("unknown activity type")
    with _app(status=None, raises=refused) as client:
        response = _attach(client)

    (frame,) = _frames(response.text)
    assert frame["code"] == "stream_unavailable"
    assert "unknown activity type" in frame["message"]


def test_closed_run_that_streams_nothing_says_so() -> None:
    # The silent case: no exception anywhere, 394 events on record, and not one delivered.
    with _app(status=WorkflowExecutionStatus.COMPLETED, published=394) as client:
        response = _attach(client)

    (frame,) = _frames(response.text)
    assert frame["code"] == "unreplayable_run"
    assert "394" in frame["message"]


def test_running_session_with_nothing_new_stays_silent() -> None:
    # The ordinary caught-up attach. Crying wolf here would put an error on every idle poll.
    with _app(status=WorkflowExecutionStatus.RUNNING, published=394) as client:
        response = _attach(client)

    assert _frames(response.text) == []


def test_closed_run_the_reader_already_holds_stays_silent() -> None:
    with _app(status=WorkflowExecutionStatus.COMPLETED, published=394) as client:
        response = _attach(client, from_offset=394)

    assert _frames(response.text) == []


@pytest.mark.parametrize("delivered", [1, 5])
def test_a_stream_that_delivered_is_left_alone(delivered: int) -> None:
    frames = [b'event: reply\ndata: {"type":"reply"}\n\n'] * delivered
    with _app(status=WorkflowExecutionStatus.COMPLETED, published=394, frames=frames) as client:
        response = _attach(client)

    assert all("code" not in frame for frame in _frames(response.text))
