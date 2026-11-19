"""``request_id`` on ``POST /api/messages``: a caller's idempotency key for one delivery.

A chat platform redelivers webhooks routinely, so an integration passes the platform's own event
id and a redelivery re-issues the same update instead of dispatching the message twice.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from temporal_agent_kit.web import AgentRegistry, create_agent_kit_app


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
    monkeypatch.setattr("temporal_agent_kit.web.app.AgentClient", _RecordingAgentClient)
    app = create_agent_kit_app(registry=AgentRegistry())
    app.state.temporal = object()
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
