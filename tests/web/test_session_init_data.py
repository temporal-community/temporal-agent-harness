"""Starting a session for an agent that takes init data.

A registry entry's start interface (read from the agent's generated schema document, never by
importing the agent) says what its ``@agent.init`` takes after its config. ``GET /api/agents``
publishes it (required or optional, and the data model's JSON Schema) so a UI can render a form,
and ``POST /api/sessions`` refuses data the agent does not take or a missing required one before
asking the session manager, which passes ``data`` to the agent as its second workflow argument.
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
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_schema import (
    agent_init_data_schema,
    dump_agent_schema,
    load_agent_class,
)
from temporal_agent_harness.web import (
    AgentDescriptor,
    AgentRegistry,
    AgentStart,
    create_agent_harness_app,
    load_agent_registry,
)
from temporal_agent_harness.web.agent_starts import agent_starts
from temporal_agent_harness.web.task_queue_status import RegistryWorkers
from temporal_agent_harness.web.session_manager import (
    CreateSessionRequest,
    Session,
    SessionManagerWorkflow,
)


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


def _descriptor(key: str, workflow_type: str, cls: type | None) -> AgentDescriptor:
    return AgentDescriptor(
        key=key,
        workflow_type=workflow_type,
        task_queue=f"{key}-q",
        label=key,
        description=key,
        start=None if cls is None else AgentStart(init_data=agent_init_data_schema(cls)),
    )


REGISTRY = AgentRegistry(
    agents=[
        _descriptor("briefed", "BriefedAgent", BriefedAgent),
        _descriptor(
            "tictactoe",
            "TicTacToeAgent",
            load_agent_class("examples.tictactoe.workflow:TicTacToeAgentWorkflow"),
        ),
        _descriptor(
            "react", "ReactAgent", load_agent_class("examples.react_agent.workflow:ReactAgentWorkflow")
        ),
        _descriptor("unnamed", "UnnamedAgent", None),
    ]
)


class _FakeManagerHandle:
    def __init__(self) -> None:
        self.requests: list[CreateSessionRequest] = []

    async def query(self, _query, **_kwargs) -> AgentRegistry:
        return REGISTRY

    async def execute_update(self, _update, request: CreateSessionRequest, **_kwargs) -> Session:
        self.requests.append(request)
        return Session(
            workflow_id=f"agent-session-{len(self.requests)}",
            created_at=0.0,
            label="Session",
            agent_workflow_type=request.agent_workflow_type,
        )


class _FakeTemporalClient:
    def get_workflow_handle(self, _workflow_id):
        return _FakeWorkflowHandle()


class _FakeWorkflowHandle:
    async def describe(self):
        raise RPCError("not found", RPCStatusCode.NOT_FOUND, b"")

    async def fetch_history_events(self, **_kwargs):
        return
        yield  # pragma: no cover - makes this an async generator


@pytest.fixture
def app_and_manager():
    app = create_agent_harness_app(registry=REGISTRY)
    manager = _FakeManagerHandle()
    app.state.temporal = _FakeTemporalClient()
    app.state.manager_handle = manager
    app.state.agent_starts = agent_starts(REGISTRY)
    return app, manager


def _start(app, workflow_type: str, data: Any = None, *, send_data: bool = True):
    body: dict[str, Any] = {"agent_workflow_type": workflow_type}
    if send_data:
        body["data"] = data
    return TestClient(app).post("/api/sessions", json=body)


def test_agents_list_publishes_each_agents_init_data(app_and_manager, monkeypatch) -> None:
    app, _ = app_and_manager

    async def no_workers(_client, task_queues):
        list(task_queues)
        return RegistryWorkers()

    monkeypatch.setattr("temporal_agent_harness.web.app.describe_task_queue_workers", no_workers)
    agents = {a["key"]: a for a in TestClient(app).get("/api/agents").json()["agents"]}

    assert agents["briefed"]["init_data"]["required"] is True
    assert agents["briefed"]["init_data"]["schema"]["properties"]["topic"]["type"] == "string"
    assert agents["tictactoe"]["init_data"]["required"] is False
    assert agents["react"]["init_data"] is None
    assert agents["unnamed"]["init_data"] is None


def test_valid_data_is_forwarded_to_the_manager(app_and_manager) -> None:
    app, manager = app_and_manager
    response = _start(app, "BriefedAgent", {"topic": "tides"})
    assert response.status_code == 200
    assert manager.requests[0].data == {"topic": "tides"}


@pytest.mark.parametrize(
    ("workflow_type", "data", "message"),
    [
        ("BriefedAgent", None, "requires init data (Brief)"),
        ("ReactAgent", {"topic": "x"}, "takes no init data"),
    ],
)
def test_bad_start_data_is_a_422_and_starts_nothing(
    app_and_manager, workflow_type, data, message
) -> None:
    app, manager = app_and_manager
    response = _start(app, workflow_type, data)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_init_data"
    assert message in response.json()["message"]
    assert manager.requests == []


def test_data_the_agent_takes_is_forwarded_for_the_agent_to_validate(app_and_manager) -> None:
    app, manager = app_and_manager
    assert _start(app, "BriefedAgent", {"wrong": 1}).status_code == 200
    assert manager.requests[0].data == {"wrong": 1}


def test_optional_data_may_be_omitted(app_and_manager) -> None:
    app, manager = app_and_manager
    assert _start(app, "TicTacToeAgent", send_data=False).status_code == 200
    assert manager.requests[0].data is None


def test_data_for_an_agent_whose_class_is_unknown_is_passed_through(app_and_manager) -> None:
    app, manager = app_and_manager
    assert _start(app, "UnnamedAgent", {"anything": True}).status_code == 200
    assert manager.requests[0].data == {"anything": True}


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


def test_the_manager_passes_data_as_the_second_workflow_argument(monkeypatch) -> None:
    started: list[dict[str, Any]] = []

    async def fake_start_child_workflow(workflow_type, **kwargs):
        started.append({"workflow_type": workflow_type, **kwargs})

    monkeypatch.setattr(
        "temporal_agent_harness.web.session_manager.workflow.start_child_workflow",
        fake_start_child_workflow,
    )
    monkeypatch.setattr("temporal_agent_harness.web.session_manager.workflow.uuid4", lambda: "u1")
    monkeypatch.setattr("temporal_agent_harness.web.session_manager.workflow.time", lambda: 0.0)
    manager = SessionManagerWorkflow(REGISTRY)
    config = AgentConfig()

    asyncio.run(
        manager.create_session(
            CreateSessionRequest(
                agent_workflow_type="BriefedAgent", config=config, data={"topic": "tides"}
            )
        )
    )
    asyncio.run(manager.create_session(CreateSessionRequest(agent_workflow_type="ReactAgent", config=config)))

    assert started[0]["args"] == [config, {"topic": "tides"}]
    assert started[1]["args"] == [config]
