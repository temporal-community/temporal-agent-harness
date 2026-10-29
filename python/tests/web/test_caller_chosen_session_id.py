"""``POST /api/sessions`` with a caller-chosen ``session_id``.

The default (no ``session_id``) mints ``agent-session-{uuid4}``, which is right when a session
has no identity outside the harness — that is what the UI uses. An integration whose
conversation identity is minted elsewhere (a Slack thread) needs the opposite: a session id
*derivable* from that identity, so it never has to keep an external-id -> session-id table.
These tests pin that option and its idempotency, which is what lets a chat webhook handler ask
every time without first checking (it could not check without racing its own redeliveries).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import (
    AgentDescriptor,
    AgentRegistry,
    create_agent_harness_app,
)
from temporal_agent_harness.web.session_manager import (
    CreateSessionRequest,
    Session,
    SessionManagerWorkflow,
)

REGISTRY = AgentRegistry(
    agents=[
        AgentDescriptor(
            key="react",
            workflow_type="ReactAgent",
            task_queue="react-agent",
            label="React",
            description="Reacts.",
        ),
    ]
)


class _FakeManagerHandle:
    """Stands in for the session manager, recording what the endpoint asked it for."""

    def __init__(self) -> None:
        self.requests: list[CreateSessionRequest] = []
        self.started: dict[str, Session] = {}
        self.minted = 0

    async def query(self, _query, **_kwargs) -> AgentRegistry:
        return REGISTRY

    async def execute_update(self, _update, request: CreateSessionRequest, **_kwargs) -> Session:
        self.requests.append(request)
        # Mirrors SessionManagerWorkflow.create_session: a named session already started is
        # returned as-is; otherwise an id is minted.
        if request.session_id and request.session_id in self.started:
            return self.started[request.session_id]
        self.minted += 1
        workflow_id = request.session_id or f"agent-session-minted-{self.minted}"
        session = Session(
            workflow_id=workflow_id,
            created_at=0.0,
            label=f"Session {self.minted}",
            agent_workflow_type=request.agent_workflow_type,
        )
        self.started[workflow_id] = session
        return session


@pytest.fixture
def app_and_manager():
    app = create_agent_harness_app(registry=REGISTRY)
    manager = _FakeManagerHandle()
    app.state.temporal = _FakeTemporalClient()
    app.state.manager_handle = manager
    return app, manager


class _FakeTemporalClient:
    """The endpoint enriches its reply by describing the new workflow. That is not what these
    tests are about, so the describe reports NOT_FOUND — the handled path — and the response
    still carries the ``workflow_id`` the assertions read."""

    def get_workflow_handle(self, _workflow_id):
        return _FakeWorkflowHandle()


class _FakeWorkflowHandle:
    async def describe(self):
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def fetch_history_events(self, **_kwargs):
        return
        yield  # pragma: no cover - makes this an async generator


def test_omitting_session_id_keeps_the_minted_default(app_and_manager) -> None:
    app, manager = app_and_manager

    response = TestClient(app).post("/api/sessions", json={"agent_workflow_type": "ReactAgent"})

    assert response.status_code == 200
    assert manager.requests[0].session_id is None
    assert response.json()["workflow_id"].startswith("agent-session-")


def test_caller_chosen_session_id_is_used_verbatim(app_and_manager) -> None:
    app, manager = app_and_manager
    chosen = "slack:C123:1699.001"

    response = TestClient(app).post(
        "/api/sessions", json={"agent_workflow_type": "ReactAgent", "session_id": chosen}
    )

    assert manager.requests[0].session_id == chosen
    assert response.json()["workflow_id"] == chosen


def test_creating_the_same_session_twice_returns_the_first(app_and_manager) -> None:
    """The property a chat integration depends on: ask on every inbound event, get the same
    session back, never a duplicate and never an error."""
    app, manager = app_and_manager
    body = {"agent_workflow_type": "ReactAgent", "session_id": "slack:C1:1"}

    client = TestClient(app)
    first = client.post("/api/sessions", json=body)
    second = client.post("/api/sessions", json=body)

    assert first.json()["workflow_id"] == second.json()["workflow_id"] == "slack:C1:1"
    assert manager.minted == 1, "the second request must not start a second workflow"


def test_session_manager_returns_the_existing_session_for_a_known_id() -> None:
    """The idempotency itself lives in the workflow, not the endpoint — assert it there."""
    manager = SessionManagerWorkflow(REGISTRY)
    existing = Session(
        workflow_id="slack:C1:1",
        created_at=0.0,
        label="Session 1",
        agent_workflow_type="ReactAgent",
    )
    manager._sessions.append(existing)

    result = asyncio.run(
        manager.create_session(
            CreateSessionRequest(
                agent_workflow_type="ReactAgent",
                config=_AnyConfig(),
                session_id="slack:C1:1",
            )
        )
    )

    # Returned without touching Temporal at all — no child workflow start was attempted.
    assert result is existing


class _AnyConfig:
    """`create_session` only forwards the config; these tests never reach the forwarding."""


# ---------------------------------------------------------------------------
# request_id -> update_id
# ---------------------------------------------------------------------------


class _RecordingAgentClient:
    """Captures what the message routes forward to the harness client."""

    calls: list[dict] = []

    def __init__(self, temporal=None, workflow_id: str = "") -> None:
        self.workflow_id = workflow_id

    async def submit_message(self, msg_type, payload, *, update_id=None):
        _RecordingAgentClient.calls.append(
            {"msg_type": msg_type, "payload": payload, "update_id": update_id}
        )

        from dataclasses import dataclass

        @dataclass
        class _Reply:
            message_id: str = "m1"
            turn_id: str = "t1"
            turn_number: int = 1

        return _Reply()


@pytest.fixture
def messages_app(monkeypatch):
    _RecordingAgentClient.calls = []
    monkeypatch.setattr("temporal_agent_harness.web.app.AgentClient", _RecordingAgentClient)
    app = create_agent_harness_app(registry=REGISTRY)
    app.state.temporal = _FakeTemporalClient()
    app.state.manager_handle = _FakeManagerHandle()
    return app


def test_request_id_becomes_a_namespaced_update_id(messages_app) -> None:
    """A redelivered chat webhook must re-issue the SAME update, not dispatch twice. The
    namespace keeps a caller's id from colliding with one the harness mints itself."""
    TestClient(messages_app).post(
        "/api/messages",
        json={"session_id": "chat-slack:C1:1", "message": "hi", "request_id": "Ev0XYZ"},
    )

    assert _RecordingAgentClient.calls[0]["update_id"] == "msg-Ev0XYZ"


def test_omitting_request_id_lets_temporal_mint_one(messages_app) -> None:
    """The UI's case: a click is one delivery, so every send is its own dispatch."""
    TestClient(messages_app).post(
        "/api/messages", json={"session_id": "chat-slack:C1:1", "message": "hi"}
    )

    assert _RecordingAgentClient.calls[0]["update_id"] is None
