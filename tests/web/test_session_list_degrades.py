# ABOUTME: /api/sessions is polled continuously and fans out one describe per session, so a single
# slow describe among forty used to fail the whole list and empty the sidebar. Timeouts here are the
# expected case, not a theoretical one — the list has to degrade one row at a time, and a visibility
# outage has to cost only the sessions discovery would have ADDED.

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app
from temporal_agent_harness.web.session_manager import AgentDescriptor, Session

HEALTHY = "agent-session-healthy"
SLOW = "agent-session-slow"
DISCOVERED = "agent-session-discovered"

REGISTRY = AgentRegistry(
    agents=[
        AgentDescriptor(
            key="hello",
            workflow_type="HelloAgent",
            task_queue="hello",
            label="Hello",
            description="",
        )
    ]
)


def _session(workflow_id: str) -> Session:
    return Session(
        workflow_id=workflow_id,
        created_at=0.0,
        label=workflow_id,
        agent_workflow_type="HelloAgent",
    )


class _Manager:
    async def query(self, name, result_type=None):
        # available_agents and list_sessions are told apart by what each caller asks back for.
        if result_type is AgentRegistry:
            return REGISTRY
        return [_session(HEALTHY), _session(SLOW)]


@contextmanager
def _client(*, discovery_fails: bool = False):
    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.temporal = object()
    app.state.manager_handle = _Manager()

    async def _state(_temporal, session: Session):
        if session.workflow_id == SLOW:
            raise RPCError("deadline exceeded", RPCStatusCode.DEADLINE_EXCEEDED, b"")
        return {**session.__dict__, "execution_status": "RUNNING", "closed": False}

    async def _discover(_temporal, _registry, _known):
        if discovery_fails:
            raise RPCError("visibility down", RPCStatusCode.UNAVAILABLE, b"")
        return [_session(DISCOVERED)]

    with (
        patch("temporal_agent_harness.web.app._session_with_execution_state", _state),
        patch("temporal_agent_harness.web.app._discover_untracked_sessions", _discover),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def test_one_slow_describe_does_not_empty_the_list() -> None:
    with _client() as client:
        response = client.get("/api/sessions")

    assert response.status_code == 200
    by_id = {item["workflow_id"]: item for item in response.json()}
    assert set(by_id) == {HEALTHY, SLOW, DISCOVERED}
    assert by_id[HEALTHY]["execution_status"] == "RUNNING"


def test_the_session_that_failed_is_listed_with_its_status_withheld() -> None:
    with _client() as client:
        slow = next(
            item for item in client.get("/api/sessions").json() if item["workflow_id"] == SLOW
        )

    assert slow["execution_status"] == "UNKNOWN"
    # Not knowing is not the same as knowing it ended, and the sidebar reads `closed`.
    assert slow["closed"] is False
    # The manager's own fields survive, so the row still renders.
    assert slow["label"] == SLOW


def test_a_visibility_outage_costs_only_the_discovered_sessions() -> None:
    with _client(discovery_fails=True) as client:
        response = client.get("/api/sessions")

    assert response.status_code == 200
    assert {item["workflow_id"] for item in response.json()} == {HEALTHY, SLOW}
