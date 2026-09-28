"""Starting a session for an agent that takes init data.

A registry entry's ``agent = "module:Class"`` lets the server read what the agent's
``@agent.init`` takes after its config. ``GET /api/agents`` publishes it (required or optional,
and the data model's JSON Schema) so a UI can render a form, and ``POST /api/sessions`` checks
``data`` against it before asking the session manager, which passes it to the agent as its
second workflow argument.
"""

from __future__ import annotations

import asyncio
import logging
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
from temporal_agent_harness.web import AgentDescriptor, AgentRegistry, create_agent_harness_app
from temporal_agent_harness.web.agent_starts import resolve_agent_starts
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


def _descriptor(key: str, workflow_type: str, agent_path: str | None) -> AgentDescriptor:
    return AgentDescriptor(
        key=key,
        workflow_type=workflow_type,
        task_queue=f"{key}-q",
        label=key,
        description=key,
        agent=agent_path,
    )


REGISTRY = AgentRegistry(
    agents=[
        _descriptor("briefed", "BriefedAgent", "tests.web.test_session_init_data:BriefedAgent"),
        _descriptor("tictactoe", "TicTacToeAgent", "examples.tictactoe.workflow:TicTacToeAgentWorkflow"),
        _descriptor("react", "ReactAgent", "examples.react_agent.workflow:ReactAgentWorkflow"),
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
    app.state.agent_starts = resolve_agent_starts(REGISTRY)
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
        ("BriefedAgent", {"wrong": 1}, "Invalid init data for BriefedAgent"),
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


def test_optional_data_may_be_omitted(app_and_manager) -> None:
    app, manager = app_and_manager
    assert _start(app, "TicTacToeAgent", send_data=False).status_code == 200
    assert manager.requests[0].data is None


def test_data_for_an_agent_whose_class_is_unknown_is_passed_through(app_and_manager) -> None:
    app, manager = app_and_manager
    assert _start(app, "UnnamedAgent", {"anything": True}).status_code == 200
    assert manager.requests[0].data == {"anything": True}


def test_an_entry_naming_a_class_with_another_workflow_type_is_rejected() -> None:
    registry = AgentRegistry(
        agents=[_descriptor("x", "NotTicTacToe", "examples.tictactoe.workflow:TicTacToeAgentWorkflow")]
    )
    with pytest.raises(ValueError, match="is the workflow 'TicTacToeAgent'"):
        resolve_agent_starts(registry)


def test_a_class_that_fails_to_import_is_logged_and_left_unknown(caplog) -> None:
    registry = AgentRegistry(agents=[_descriptor("x", "Missing", "no.such.module:Agent")])
    with caplog.at_level(logging.WARNING):
        assert resolve_agent_starts(registry) == {}
    assert "could not import" in caplog.text


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
