from __future__ import annotations

from fastapi.testclient import TestClient

from examples.nexus_hello import subagent_server


def _message(message_id: str, text: str, *, task: dict | None = None) -> dict:
    message = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if task:
        message.update(taskId=task["id"], contextId=task["contextId"])
    return {"message": message}


def test_publishes_standard_a2a_agent_card() -> None:
    client = TestClient(subagent_server.app)
    response = client.get("/.well-known/agent-card.json")

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Writer HTTP"
    assert card["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"
    assert card["skills"][0]["id"] == "ask"


def test_first_message_creates_a2a_task_and_returns_answer() -> None:
    client = TestClient(subagent_server.app)
    response = client.post(
        "/message:send",
        headers={"A2A-Version": "1.0"},
        json=_message("message-1", "hello"),
    )

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["id"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "hello" in task["artifacts"][-1]["parts"][0]["text"]


def test_followup_message_reuses_the_same_a2a_task() -> None:
    client = TestClient(subagent_server.app)
    first = client.post(
        "/message:send",
        headers={"A2A-Version": "1.0"},
        json=_message("message-2", "first"),
    ).json()["task"]
    second_response = client.post(
        "/message:send",
        headers={"A2A-Version": "1.0"},
        json=_message("message-3", "second", task=first),
    )

    assert second_response.status_code == 200
    second = second_response.json()["task"]
    assert second["id"] == first["id"]
    assert "second" in second["artifacts"][-1]["parts"][0]["text"]
