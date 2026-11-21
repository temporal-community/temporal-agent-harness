"""Starting a session for an agent that takes init data.

A registry entry's start interface (read from the agent's generated schema document, never by
importing the agent) says what its ``@agent.init`` takes after its config. ``GET /api/agents``
publishes it (required or optional, and the data model's JSON Schema) so a UI can render a form,
and ``POST /api/sessions`` refuses data the agent does not take or a missing required one before
starting the agent workflow with ``data`` as its second argument.
The agent validates the data itself.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from temporalio.contrib.workflow_streams import WorkflowStream

from temporal_agent_kit import AgentWorkflowRunner, agent
from temporal_agent_kit.agent_protocol import (
    AgentConfig,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_kit.agent_schema import (
    agent_init_data_schema,
    dump_agent_schema,
    load_agent_class,
)
from temporal_agent_kit.web import (
    AgentDescriptor,
    AgentStart,
    create_agent_kit_app,
    load_agent_registry,
)
from temporal_agent_kit.web import app as app_module
from temporal_agent_kit.web import sessions
from temporal_agent_kit.web.memory_registry_store import (
    InMemoryRegistryStore,
    in_memory_registry_store,
)
from temporal_agent_kit.web.registry import registry_view
from temporal_agent_kit.web.registry_store import RegisteredAgent, RegistrySnapshot
from temporal_agent_kit.web.sessions import Session
from temporal_agent_kit.web.task_queue_status import RegistryWorkers


class Brief(BaseModel):
    """What the agent is working on."""

    topic: str


@agent.defn(name="BriefedAgent")
class BriefedAgent:
    @agent.init
    def __init__(self, config: AgentConfig, brief: Brief) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask."""
        return TextReply(text=message.text)


def _registered(key: str, workflow_type: str, cls: type | None) -> RegisteredAgent:
    return RegisteredAgent(
        key=key,
        workflow_type=workflow_type,
        task_queue=f"{key}-q",
        label=key,
        description=key,
        start=None if cls is None else AgentStart(init_data=agent_init_data_schema(cls)),
    )


SNAPSHOT = RegistrySnapshot.of(
    [
        _registered("briefed", "BriefedAgent", BriefedAgent),
        _registered(
            "tictactoe",
            "TicTacToeAgent",
            load_agent_class("examples.tictactoe.workflow:TicTacToeAgentWorkflow"),
        ),
        _registered(
            "react", "ReactAgent", load_agent_class("examples.react_agent.workflow:ReactAgentWorkflow")
        ),
        _registered("unnamed", "UnnamedAgent", None),
    ]
)
REGISTRY = registry_view(SNAPSHOT)


class _StartRecorder:
    """Stands in for ``start_session``, recording what the endpoint asked it to start."""

    def __init__(self) -> None:
        self.data: list[dict[str, Any] | None] = []

    async def __call__(self, _temporal, agent: AgentDescriptor, _registry, *, data, session_id):
        self.data.append(data)
        return Session(
            workflow_id=f"agent-session-{len(self.data)}",
            agent_workflow_type=agent.workflow_type,
            label=agent.label,
            created_at=0.0,
            execution_status="RUNNING",
            closed=False,
        )


@pytest.fixture
def app_and_starts(monkeypatch):
    store = InMemoryRegistryStore(SNAPSHOT)
    app = create_agent_kit_app(registry_store=in_memory_registry_store(store))
    starts = _StartRecorder()
    monkeypatch.setattr(app_module, "start_session", starts)
    app.state.temporal = object()
    app.state.registry_store = store
    return app, starts


def _start(app, workflow_type: str, data: Any = None, *, send_data: bool = True):
    body: dict[str, Any] = {"agent_workflow_type": workflow_type}
    if send_data:
        body["data"] = data
    return TestClient(app).post("/api/sessions", json=body)


def test_agents_list_publishes_each_agents_init_data(app_and_starts, monkeypatch) -> None:
    app, _ = app_and_starts

    async def no_workers(_client, task_queues):
        list(task_queues)
        return RegistryWorkers()

    monkeypatch.setattr("temporal_agent_kit.web.app.describe_task_queue_workers", no_workers)
    agents = {a["key"]: a for a in TestClient(app).get("/api/agents").json()["agents"]}

    assert agents["briefed"]["init_data"]["required"] is True
    assert agents["briefed"]["init_data"]["schema"]["properties"]["topic"]["type"] == "string"
    assert agents["tictactoe"]["init_data"]["required"] is False
    assert agents["react"]["init_data"] is None
    assert agents["unnamed"]["init_data"] is None


def test_valid_data_is_forwarded_to_the_agent(app_and_starts) -> None:
    app, starts = app_and_starts
    response = _start(app, "BriefedAgent", {"topic": "tides"})
    assert response.status_code == 200
    assert starts.data[0] == {"topic": "tides"}


@pytest.mark.parametrize(
    ("workflow_type", "data", "message"),
    [
        ("BriefedAgent", None, "requires init data (Brief)"),
        ("ReactAgent", {"topic": "x"}, "takes no init data"),
    ],
)
def test_bad_start_data_is_a_422_and_starts_nothing(
    app_and_starts, workflow_type, data, message
) -> None:
    app, starts = app_and_starts
    response = _start(app, workflow_type, data)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_init_data"
    assert message in response.json()["message"]
    assert starts.data == []


def test_data_the_agent_takes_is_forwarded_for_the_agent_to_validate(app_and_starts) -> None:
    app, starts = app_and_starts
    assert _start(app, "BriefedAgent", {"wrong": 1}).status_code == 200
    assert starts.data[0] == {"wrong": 1}


def test_optional_data_may_be_omitted(app_and_starts) -> None:
    app, starts = app_and_starts
    assert _start(app, "TicTacToeAgent", send_data=False).status_code == 200
    assert starts.data[0] is None


def test_data_for_an_agent_whose_class_is_unknown_is_passed_through(app_and_starts) -> None:
    app, starts = app_and_starts
    assert _start(app, "UnnamedAgent", {"anything": True}).status_code == 200
    assert starts.data[0] == {"anything": True}


def _registry_file(tmp_path: Path, entry: str) -> Path:
    path = tmp_path / "agents.toml"
    path.write_text(
        "[[agents]]\n"
        'key = "briefed"\n'
        'workflow_type = "BriefedAgent"\n'
        'task_queue = "briefed-q"\n'
        'label = "Briefed"\n'
        'description = "Takes a brief."\n' + entry
    )
    return path


def test_a_registry_reads_each_agents_start_from_its_schema_file(tmp_path: Path) -> None:
    (tmp_path / "BriefedAgent.schema.json").write_text(dump_agent_schema(BriefedAgent))
    registry = load_agent_registry(_registry_file(tmp_path, 'schema = "BriefedAgent.schema.json"\n'))
    assert registry.agents[0].start == AgentStart(init_data=agent_init_data_schema(BriefedAgent))


def test_a_registry_entry_without_a_schema_has_an_unknown_start(tmp_path: Path) -> None:
    assert load_agent_registry(_registry_file(tmp_path, "")).agents[0].start is None


def test_a_schema_for_another_workflow_type_is_rejected(tmp_path: Path) -> None:
    schema = json.loads(dump_agent_schema(BriefedAgent)) | {"agent": "SomeoneElse"}
    (tmp_path / "x.schema.json").write_text(json.dumps(schema))
    with pytest.raises(ValueError, match="describes the workflow 'SomeoneElse'"):
        load_agent_registry(_registry_file(tmp_path, 'schema = "x.schema.json"\n'))


def test_data_is_the_agent_workflows_second_argument(monkeypatch) -> None:
    started: list[dict[str, Any]] = []

    class FakeTemporal:
        async def start_workflow(self, workflow_type, **kwargs):
            started.append({"workflow_type": workflow_type, **kwargs})
            return type("Handle", (), {"id": kwargs["id"]})()

    async def fake_get_session(_temporal, _registry, session_id):
        return Session(session_id, "BriefedAgent", "briefed", 0.0, "RUNNING", False)

    monkeypatch.setattr(sessions, "get_session", fake_get_session)
    briefed, react = REGISTRY.by_key("briefed"), REGISTRY.by_key("react")
    assert briefed is not None and react is not None

    asyncio.run(sessions.start_session(FakeTemporal(), briefed, REGISTRY, data={"topic": "tides"}))
    asyncio.run(sessions.start_session(FakeTemporal(), react, REGISTRY))

    assert started[0]["args"] == [AgentConfig(), {"topic": "tides"}]
    assert started[0]["task_queue"] == "briefed-q"
    assert started[1]["args"] == [AgentConfig()]
