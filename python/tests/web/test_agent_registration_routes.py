"""Registering, retiring and deleting agents through the HTTP API the console and the CLI share."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from temporal_agent_kit.web import AgentRegistration, create_agent_kit_app
from temporal_agent_kit.web import app as app_module
from temporal_agent_kit.web.memory_registry_store import (
    InMemoryRegistryStore,
    in_memory_registry_store,
)
from temporal_agent_kit.web.registry_changes import RegistryConflict
from temporal_agent_kit.web.registry_store import RegistrySnapshot
from temporal_agent_kit.web.sessions import Session
from temporal_agent_kit.web.task_queue_status import RegistryWorkers

SCHEMA: dict[str, Any] = {
    "agent": "BriefedAgent",
    "source": "agents:BriefedAgent",
    "init_data": {"data": {"$ref": "#/$defs/Brief"}, "required": True},
    "handlers": {},
    "states": {},
    "$defs": {
        "Brief": {
            "title": "Brief",
            "type": "object",
            "properties": {"topic": {"title": "Topic", "type": "string"}},
            "required": ["topic"],
        }
    },
}


def _registration(key: str = "briefed", **fields: Any) -> dict[str, Any]:
    return {
        "key": key,
        "workflow_type": "BriefedAgent" if key == "briefed" else f"{key.title()}Agent",
        "task_queue": f"{key}-queue",
        "label": key.title(),
        "description": f"The {key} agent.",
        **fields,
    }


@pytest.fixture
def store() -> InMemoryRegistryStore:
    return InMemoryRegistryStore()


@pytest.fixture
def app(store: InMemoryRegistryStore, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    async def no_workers(_client, task_queues):
        list(task_queues)
        return RegistryWorkers()

    async def started(_temporal, agent, _registry, *, data, session_id):
        return Session("agent-session-1", agent.workflow_type, agent.label, 0.0, "RUNNING", False)

    monkeypatch.setattr(app_module, "describe_task_queue_workers", no_workers)
    monkeypatch.setattr(app_module, "start_session", started)
    app = create_agent_kit_app(registry_store=in_memory_registry_store(store))
    app.state.temporal = object()
    app.state.registry_store = store
    return app


def _agents(client: TestClient) -> dict[str, dict[str, Any]]:
    return {agent["key"]: agent for agent in client.get("/api/agents").json()["agents"]}


def test_registered_agents_are_listed_with_their_init_data(app: FastAPI) -> None:
    client = TestClient(app)

    response = client.post(
        "/api/agents", json={"agents": [_registration(schema=SCHEMA), _registration("plain")]}
    )

    assert response.status_code == 200, response.text
    assert sorted(agent["key"] for agent in response.json()["agents"]) == ["briefed", "plain"]
    agents = _agents(client)
    assert agents["briefed"]["init_data"]["required"] is True
    assert agents["briefed"]["init_data"]["schema"]["title"] == "Brief"
    assert agents["briefed"]["retired"] is False
    assert agents["plain"]["init_data"] is None


def test_an_agents_registration_is_given_back_as_registered(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the server's prefix on its queue, so sending it back registers the same agent."""
    monkeypatch.setenv("TASK_QUEUE_PREFIX", "ws-1")
    client = TestClient(app)
    briefed, plain = _registration(schema=SCHEMA), _registration("plain")
    client.post("/api/agents", json={"agents": [briefed, plain]})

    assert client.get("/api/agents/briefed/registration").json() == briefed
    assert client.get("/api/agents/plain/registration").json() == plain | {"schema": None}
    assert client.get("/api/agents/nobody/registration").status_code == 404


def test_a_registration_the_rules_refuse_registers_nothing(app: FastAPI) -> None:
    client = TestClient(app)
    client.post("/api/agents", json={"agents": [_registration("taken", workflow_type="SameAgent")]})

    response = client.post(
        "/api/agents",
        json={"agents": [_registration("fresh"), _registration("other", workflow_type="SameAgent")]},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "registry_conflict"
    assert sorted(_agents(client)) == ["taken"]


def test_a_schema_for_another_workflow_is_a_422(app: FastAPI) -> None:
    response = TestClient(app).post(
        "/api/agents", json={"agents": [_registration(schema=SCHEMA | {"agent": "SomeoneElse"})]}
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_registration"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"agents": []}, id="no agents"),
        pytest.param({"agents": [_registration(label="")]}, id="empty label"),
        pytest.param({"agents": [{"key": "only-a-key"}]}, id="missing fields"),
    ],
)
def test_a_malformed_body_is_a_422(app: FastAPI, body: dict[str, Any]) -> None:
    assert TestClient(app).post("/api/agents", json=body).status_code == 422


def test_a_retired_agent_is_listed_but_starts_nothing_until_registered_again(app: FastAPI) -> None:
    client = TestClient(app)
    client.post("/api/agents", json={"agents": [_registration("plain")]})

    assert client.post("/api/agents/plain/retire").status_code == 200
    assert _agents(client)["plain"]["retired"] is True
    refused = client.post("/api/sessions", json={"agent_workflow_type": "PlainAgent"})
    assert refused.status_code == 409
    assert "retired" in refused.json()["detail"]

    client.post("/api/agents", json={"agents": [_registration("plain")]})
    assert _agents(client)["plain"]["retired"] is False
    assert client.post("/api/sessions", json={"agent_workflow_type": "PlainAgent"}).status_code == 200


def test_a_deleted_agent_is_gone(app: FastAPI) -> None:
    client = TestClient(app)
    client.post("/api/agents", json={"agents": [_registration("plain")]})

    assert client.delete("/api/agents/plain").status_code == 200

    assert _agents(client) == {}
    assert client.post("/api/sessions", json={"agent_workflow_type": "PlainAgent"}).status_code == 422


@pytest.mark.parametrize("method,path", [("post", "/api/agents/nobody/retire"), ("delete", "/api/agents/nobody")])
def test_retiring_or_deleting_an_unknown_agent_is_a_404(app: FastAPI, method: str, path: str) -> None:
    response = getattr(TestClient(app), method)(path)
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_agent"


def test_the_task_queue_is_stored_as_registered_and_served_under_the_prefix(
    app: FastAPI, store: InMemoryRegistryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TASK_QUEUE_PREFIX", "ws-1")
    client = TestClient(app)
    client.post("/api/agents", json={"agents": [_registration("plain")]})

    assert _agents(client)["plain"]["task_queue"] == "ws-1-plain-queue"
    stored = store._stored.snapshot.agents["plain"]
    assert stored.task_queue == "plain-queue"


async def test_startup_registrations_are_in_the_registry_once_the_app_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connect(**_config: Any) -> object:
        return object()

    monkeypatch.setattr(app_module.ClientConfig, "load_client_connect_config", lambda: {})
    monkeypatch.setattr(app_module.Client, "connect", connect)
    store = InMemoryRegistryStore(RegistrySnapshot.of([]))
    app = create_agent_kit_app(
        registry_store=in_memory_registry_store(store),
        registrations=[AgentRegistration.model_validate(_registration(schema=SCHEMA))],
    )

    async with app.router.lifespan_context(app):
        stored = await store.read()

    assert list(stored.snapshot.agents) == ["briefed"]
    assert stored.snapshot.agents["briefed"].start is not None


def test_startup_registrations_the_rules_refuse_fail_before_the_app_is_built() -> None:
    with pytest.raises(RegistryConflict):
        create_agent_kit_app(
            registry_store=in_memory_registry_store(),
            registrations=[
                AgentRegistration.model_validate(_registration("a", workflow_type="Same")),
                AgentRegistration.model_validate(_registration("b", workflow_type="Same")),
            ],
        )


def test_a_registration_takes_its_schema_under_the_schema_key() -> None:
    registration = AgentRegistration.model_validate(_registration(schema=SCHEMA))
    assert registration.schema_document == SCHEMA
    assert json.loads(registration.model_dump_json(by_alias=True))["schema"] == SCHEMA
