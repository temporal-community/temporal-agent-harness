from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from examples.nexus_hello import subagent_server


@pytest.fixture(autouse=True)
def reset_server_state() -> None:
    subagent_server._start_keys.clear()
    subagent_server._sessions.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(subagent_server.app)


def _start(client: TestClient, key: str) -> str:
    response = client.post("/sessions", json={"idempotency_key": key})
    assert response.status_code == 200
    return response.json()["instance_id"]


def _turn(client: TestClient, instance_id: str, text: str, turn: int):
    return client.post(
        f"/sessions/{instance_id}/turns",
        json={
            "idempotency_key": f"agent:writer:{instance_id}:{turn}",
            "msg_type": "ask",
            "payload": f'{{"text": "{text}"}}',
            "expected_turn": turn,
        },
    )


def test_start_is_idempotent(client: TestClient) -> None:
    assert _start(client, "start-1") == _start(client, "start-1")


def test_instances_have_independent_turn_state(client: TestClient) -> None:
    first = _start(client, "start-1")
    second = _start(client, "start-2")
    assert first != second

    first_reply = _turn(client, first, "session A", 1)
    second_reply = _turn(client, second, "session B", 1)

    assert first_reply.status_code == 200
    assert second_reply.status_code == 200
    assert "session A" in first_reply.json()["output"]["text"]
    assert "session B" in second_reply.json()["output"]["text"]
    assert first_reply.json()["turn_number"] == 1
    assert second_reply.json()["turn_number"] == 1


def test_turn_retry_returns_the_first_result(client: TestClient) -> None:
    instance_id = _start(client, "start-1")
    first = _turn(client, instance_id, "hello", 1)
    retry = _turn(client, instance_id, "hello", 1)

    assert retry.status_code == 200
    assert retry.json() == first.json()


def test_stale_turn_is_rejected(client: TestClient) -> None:
    instance_id = _start(client, "start-1")
    response = _turn(client, instance_id, "wrong turn", 2)
    assert response.status_code == 409


def test_close_removes_only_the_selected_instance(client: TestClient) -> None:
    first = _start(client, "start-1")
    second = _start(client, "start-2")

    assert client.post(f"/sessions/{first}/close").status_code == 200
    assert _turn(client, first, "closed", 1).status_code == 404
    assert _turn(client, second, "open", 1).status_code == 200
