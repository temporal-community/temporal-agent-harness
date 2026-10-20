from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_protocol import (
    AgentMessage,
    SEND_AGENT_MESSAGE_UPDATE,
)
from temporal_agent_harness.ui import packaged_ui_dist
from temporal_agent_harness.web import (
    SESSION_MANAGER_TASK_QUEUE,
    AgentDescriptor,
    AgentRegistry,
    Session,
    SessionManagerWorkflow,
    create_agent_harness_app,
    create_session_manager_worker,
)
from temporal_agent_harness.web.app import (
    _discover_untracked_sessions,
    _ensure_session_manager_workflow,
    _session_with_execution_state,
    _workflow_execution_state,
)

ROOT = Path(__file__).resolve().parents[2]


def test_packaged_ui_dist_contains_relative_vite_entrypoints() -> None:
    dist = packaged_ui_dist()

    assert dist is not None
    index = dist / "index.html"
    assert index.is_file()

    html = index.read_text()
    asset_paths = _relative_asset_paths(html)
    assert any(path.endswith(".js") for path in asset_paths)
    assert any(path.endswith(".css") for path in asset_paths)
    assert 'src="/' not in html
    assert 'href="/' not in html
    for asset_path in asset_paths:
        assert (dist / asset_path).is_file()


def test_app_built_from_a_registry_path_serves_packaged_svelte_ui() -> None:
    from temporal_agent_harness.web.serve import create_app

    app = create_app(ROOT / "examples" / "monty" / "agents.toml")
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "<title>Temporal Agent UI</title>" in response.text
    assert 'src="./assets/' in response.text
    assert 'href="./assets/' in response.text


def test_packaged_svelte_ui_works_under_path_prefix() -> None:
    subapp = create_agent_harness_app(registry=AgentRegistry())
    parent = FastAPI()
    parent.mount("/harness", subapp)
    client = TestClient(parent)

    index = client.get("/harness/")

    assert index.status_code == 200
    asset_paths = _relative_asset_paths(index.text)
    assert asset_paths

    for asset_path in asset_paths:
        asset = client.get(f"/harness/{asset_path}")
        assert asset.status_code == 200

    logo = client.get("/harness/temporal-logo.svg")
    assert logo.status_code == 200
    assert client.get("/harness/states").status_code == 404


def test_chat_request_rejects_client_supplied_from_offset() -> None:
    app = create_agent_harness_app(registry=AgentRegistry())
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={
            "session_id": "agent-session-test",
            "message": "hello",
            "from_offset": 42,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("from_offset" in item.get("loc", []) for item in detail)


def test_submit_message_request_rejects_client_supplied_from_offset() -> None:
    app = create_agent_harness_app(registry=AgentRegistry())
    client = TestClient(app)

    response = client.post(
        "/api/messages",
        json={
            "session_id": "agent-session-test",
            "message": "hello",
            "from_offset": 42,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("from_offset" in item.get("loc", []) for item in detail)


def test_legacy_session_manager_example_folder_is_removed() -> None:
    legacy_example = ROOT / "examples" / "session_manager"

    assert not legacy_example.exists()


def test_create_session_manager_worker_registers_packaged_workflow() -> None:
    client = object()

    with patch("temporal_agent_harness.web.session_manager_worker.Worker") as worker_cls:
        worker = create_session_manager_worker(client, identity="session-manager-test")

    assert worker is worker_cls.return_value
    worker_cls.assert_called_once_with(
        client,
        task_queue=SESSION_MANAGER_TASK_QUEUE,
        workflows=[SessionManagerWorkflow],
        identity="session-manager-test",
    )


def test_create_session_manager_worker_allows_custom_task_queue() -> None:
    client = object()

    with patch("temporal_agent_harness.web.session_manager_worker.Worker") as worker_cls:
        create_session_manager_worker(client, task_queue="custom-session-manager")

    worker_cls.assert_called_once_with(
        client,
        task_queue="custom-session-manager",
        workflows=[SessionManagerWorkflow],
    )


def test_create_session_manager_worker_rejects_owned_worker_registration() -> None:
    with pytest.raises(ValueError, match="workflows"):
        create_session_manager_worker(object(), workflows=[])

    with pytest.raises(ValueError, match="activities"):
        create_session_manager_worker(object(), activities=[])


async def test_session_manager_startup_attaches_to_running_workflow() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(handle)

    result = await _ensure_session_manager_workflow(
        temporal,
        registry=AgentRegistry(),
        manager_workflow_id="session-manager",
        manager_task_queue="session-manager",
    )

    assert result is handle
    assert temporal.start_calls == []


async def test_workflow_execution_state_reports_running_workflow_open() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(handle)

    result = await _workflow_execution_state(temporal, "agent-session-test")

    assert result == {
        "workflow_id": "agent-session-test",
        "execution_status": "RUNNING",
        "closed": False,
    }


async def test_session_execution_state_reports_completed_workflow_closed() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.COMPLETED)
    temporal = _FakeTemporalClient(handle)
    session = Session(
        workflow_id="agent-session-test",
        created_at=123.0,
        label="Session 1",
        agent_workflow_type="TestAgent",
    )

    result = await _session_with_execution_state(temporal, session)

    assert result == {
        "workflow_id": "agent-session-test",
        "created_at": 123.0,
        "label": "Session 1",
        "agent_workflow_type": "TestAgent",
        "is_discovered": False,
        "execution_status": "COMPLETED",
        "closed": True,
    }


async def test_session_execution_state_includes_initial_user_message() -> None:
    history_event = await _history_event_for_agent_message(
        AgentMessage(
            type="ask",
            payload={"text": "Seven Nation Army"},
        )
    )
    handle = _FakeWorkflowHandle(
        status=WorkflowExecutionStatus.RUNNING,
        history_events=[history_event],
    )
    temporal = _FakeTemporalClient(handle)
    session = Session(
        workflow_id="agent-session-test",
        created_at=123.0,
        label="Session 1",
        agent_workflow_type="TestAgent",
    )

    result = await _session_with_execution_state(temporal, session)

    assert result["initial_user_message"] == "Seven Nation Army"


async def test_discover_untracked_sessions_finds_running_workflow_not_in_manager_list() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.RUNNING)
    started_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
    temporal = _FakeTemporalClient(
        handle,
        workflow_executions=[
            _fake_workflow_execution(
                id="agent-session-outside",
                workflow_type="TestAgent",
                start_time=started_at,
            )
        ],
    )
    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="test",
                workflow_type="TestAgent",
                task_queue="test-agent",
                label="Test Agent",
                description="A test agent.",
            )
        ]
    )

    discovered = await _discover_untracked_sessions(temporal, registry, known_workflow_ids=set())

    assert discovered == [
        Session(
            workflow_id="agent-session-outside",
            created_at=started_at.timestamp(),
            label="Test Agent",
            agent_workflow_type="TestAgent",
            is_discovered=True,
        )
    ]
    assert temporal.list_workflows_calls == [
        {"query": "ExecutionStatus='Running' AND (WorkflowType='TestAgent')", "limit": 200}
    ]


async def test_discover_untracked_sessions_excludes_already_known_workflow_ids() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(
        handle,
        workflow_executions=[
            _fake_workflow_execution(id="agent-session-known", workflow_type="TestAgent")
        ],
    )
    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="test",
                workflow_type="TestAgent",
                task_queue="test-agent",
                label="Test Agent",
                description="A test agent.",
            )
        ]
    )

    discovered = await _discover_untracked_sessions(
        temporal, registry, known_workflow_ids={"agent-session-known"}
    )

    assert discovered == []


async def test_discover_untracked_sessions_skips_workflow_types_outside_registry() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(
        handle,
        workflow_executions=[
            _fake_workflow_execution(id="agent-session-other", workflow_type="OtherWorkflow")
        ],
    )
    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="test",
                workflow_type="TestAgent",
                task_queue="test-agent",
                label="Test Agent",
                description="A test agent.",
            )
        ]
    )

    discovered = await _discover_untracked_sessions(temporal, registry, known_workflow_ids=set())

    assert discovered == []


async def test_discover_untracked_sessions_skips_list_workflows_when_registry_is_empty() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(handle)

    discovered = await _discover_untracked_sessions(
        temporal, AgentRegistry(), known_workflow_ids=set()
    )

    assert discovered == []
    assert temporal.list_workflows_calls == []


async def test_session_manager_startup_starts_when_missing() -> None:
    handle = _FakeWorkflowHandle(
        error=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    )
    started_handle = object()
    registry = AgentRegistry()
    temporal = _FakeTemporalClient(handle, start_result=started_handle)

    result = await _ensure_session_manager_workflow(
        temporal,
        registry=registry,
        manager_workflow_id="session-manager",
        manager_task_queue="session-manager",
    )

    assert result is started_handle
    assert temporal.start_calls == [
        {
            "workflow": SessionManagerWorkflow.run,
            "arg": registry,
            "id": "session-manager",
            "task_queue": "session-manager",
            "id_conflict_policy": WorkflowIDConflictPolicy.USE_EXISTING,
        }
    ]


async def test_session_manager_startup_surfaces_temporal_rpc_failures() -> None:
    handle = _FakeWorkflowHandle(
        error=RPCError("permission denied", RPCStatusCode.PERMISSION_DENIED, b"")
    )
    temporal = _FakeTemporalClient(handle)

    with pytest.raises(RPCError, match="permission denied"):
        await _ensure_session_manager_workflow(
            temporal,
            registry=AgentRegistry(),
            manager_workflow_id="session-manager",
            manager_task_queue="session-manager",
        )

    assert temporal.start_calls == []


async def test_session_manager_startup_handles_concurrent_start_race() -> None:
    handle = _FakeWorkflowHandle(
        error=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")
    )
    temporal = _FakeTemporalClient(
        handle,
        start_error=WorkflowAlreadyStartedError(
            "session-manager",
            "SessionManagerWorkflow",
        ),
    )

    result = await _ensure_session_manager_workflow(
        temporal,
        registry=AgentRegistry(),
        manager_workflow_id="session-manager",
        manager_task_queue="session-manager",
    )

    assert result is handle
    assert temporal.get_handle_ids == ["session-manager", "session-manager"]
    assert len(temporal.start_calls) == 1


def _relative_asset_paths(index_html: str) -> set[str]:
    return set(re.findall(r'(?:src|href)="\./([^"]+)"', index_html))


class _FakeWorkflowHandle:
    def __init__(
        self,
        *,
        status: WorkflowExecutionStatus | None = None,
        error: Exception | None = None,
        history_events: list[HistoryEvent] | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.history_events = history_events or []

    async def describe(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(status=self.status)

    async def fetch_history_events(self, **_kwargs):
        for event in self.history_events:
            yield event


class _FakeTemporalClient:
    def __init__(
        self,
        handle: _FakeWorkflowHandle,
        *,
        start_result: object | None = None,
        start_error: Exception | None = None,
        workflow_executions: list[object] | None = None,
    ) -> None:
        self.handle = handle
        self.start_result = start_result
        self.start_error = start_error
        self.get_handle_ids: list[str] = []
        self.start_calls: list[dict[str, object]] = []
        self.list_workflows_calls: list[dict[str, object]] = []
        self.workflow_executions = workflow_executions or []
        self.data_converter = pydantic_data_converter

    def get_workflow_handle(self, workflow_id: str) -> _FakeWorkflowHandle:
        self.get_handle_ids.append(workflow_id)
        return self.handle

    async def start_workflow(self, workflow, arg, **kwargs):
        self.start_calls.append({"workflow": workflow, "arg": arg, **kwargs})
        if self.start_error is not None:
            raise self.start_error
        return self.start_result

    async def list_workflows(self, query: str | None = None, **kwargs):
        self.list_workflows_calls.append({"query": query, **kwargs})
        for execution in self.workflow_executions:
            yield execution


def _fake_workflow_execution(
    *, id: str, workflow_type: str, start_time: datetime | None = None
) -> object:
    return SimpleNamespace(
        id=id,
        workflow_type=workflow_type,
        start_time=start_time or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


async def _history_event_for_agent_message(message: AgentMessage) -> HistoryEvent:
    event = HistoryEvent(
        event_id=1,
        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED,
    )
    request = event.workflow_execution_update_accepted_event_attributes.accepted_request
    request.input.name = SEND_AGENT_MESSAGE_UPDATE
    request.input.args.payloads.extend(
        await pydantic_data_converter.encode([message])
    )
    return event
