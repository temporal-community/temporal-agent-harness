"""``temporal-agent-kit agents``: the CLI's calls to a running server's registry routes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi.testclient import TestClient

from temporal_agent_kit.web import agent_commands, create_agent_kit_app
from temporal_agent_kit.web import app as app_module
from temporal_agent_kit.web.agent_commands import (
    AgentCommandError,
    add_agents,
    delete_agent,
    format_agents,
    list_agents,
    retire_agent,
)
from temporal_agent_kit.web.cli import main
from temporal_agent_kit.web.memory_registry_store import (
    InMemoryRegistryStore,
    in_memory_registry_store,
)
from temporal_agent_kit.web.task_queue_status import RegistryWorkers

SERVER = "http://agent-kit.test"

REGISTRY = """
[[agents]]
key = "briefed"
workflow_type = "BriefedAgent"
task_queue = "briefed-queue"
label = "Briefed"
schema = "schemas/BriefedAgent.schema.json"
description = "Takes a brief."

[[agents]]
key = "plain"
workflow_type = "PlainAgent"
task_queue = "plain-queue"
label = "Plain"
description = "Takes nothing."
"""

SCHEMA = {
    "agent": "BriefedAgent",
    "init_data": {"data": {"$ref": "#/$defs/Brief"}, "required": True},
    "$defs": {"Brief": {"title": "Brief", "type": "object", "properties": {}}},
}


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemoryRegistryStore:
    """A server on an in-memory store, which ``urlopen`` reaches through a test client."""

    async def no_workers(_client, task_queues):
        list(task_queues)
        return RegistryWorkers()

    monkeypatch.setattr(app_module, "describe_task_queue_workers", no_workers)
    store = InMemoryRegistryStore()
    app = create_agent_kit_app(registry_store=in_memory_registry_store(store))
    app.state.temporal = object()
    app.state.registry_store = store
    client = TestClient(app)

    def urlopen(request: Request, timeout: float) -> _Response:
        assert request.full_url.startswith(SERVER)
        response = client.request(
            request.get_method(),
            request.full_url.removeprefix(SERVER),
            content=request.data,
            headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url, response.status_code, "", {}, io.BytesIO(response.content)  # type: ignore[arg-type]
            )
        return _Response(response.content)

    monkeypatch.setattr(agent_commands, "urlopen", urlopen)
    return store


@pytest.fixture
def registry_file(tmp_path: Path) -> Path:
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "BriefedAgent.schema.json").write_text(json.dumps(SCHEMA))
    path = tmp_path / "agents.toml"
    path.write_text(REGISTRY)
    return path


async def test_add_registers_a_files_agents_with_their_schema_documents(
    store: InMemoryRegistryStore, registry_file: Path
) -> None:
    assert add_agents(registry_file, SERVER) == ["briefed", "plain"]

    stored = (await store.read()).snapshot.agents
    assert stored["briefed"].schema_document == SCHEMA
    assert stored["plain"].schema_document is None


def test_list_retire_and_delete(store: InMemoryRegistryStore, registry_file: Path) -> None:
    add_agents(registry_file, SERVER)

    retire_agent("plain", SERVER)
    listed: dict[str, Any] = {agent["key"]: agent for agent in list_agents(SERVER)}
    assert listed["plain"]["retired"] is True

    delete_agent("plain", SERVER)
    assert [agent["key"] for agent in list_agents(SERVER)] == ["briefed"]


def test_a_refusal_carries_the_servers_message(store: InMemoryRegistryStore) -> None:
    with pytest.raises(AgentCommandError, match="No agent 'nobody' is registered"):
        retire_agent("nobody", SERVER)


def test_the_cli_prints_the_agents(
    store: InMemoryRegistryStore, registry_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["agents", "add", str(registry_file), "--server", SERVER])
    main(["agents", "retire", "plain", "--server", SERVER])
    main(["agents", "list", "--server", SERVER])

    out = capsys.readouterr().out
    assert "Registered briefed, plain" in out
    assert "Retired plain" in out
    assert "briefed  active   BriefedAgent  briefed-queue" in out
    assert "plain    retired  PlainAgent    plain-queue" in out


def test_the_cli_exits_with_the_servers_refusal(store: InMemoryRegistryStore) -> None:
    with pytest.raises(SystemExit, match="agents delete: .*404.*No agent 'nobody'"):
        main(["agents", "delete", "nobody", "--server", SERVER])


def test_an_empty_registry_says_so() -> None:
    assert format_agents([]) == "No agents are registered."
