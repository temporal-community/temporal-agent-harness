from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus

from temporal_agent_kit.ui import packaged_ui_dist
from temporal_agent_kit.web import AgentRegistry, create_agent_kit_app
from temporal_agent_kit.web.app import _workflow_execution_state

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
    from temporal_agent_kit.web.serve import create_app

    app = create_app(ROOT / "examples" / "monty" / "agents.toml")
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "<title>Temporal Agent UI</title>" in response.text
    assert 'src="./assets/' in response.text
    assert 'href="./assets/' in response.text


def test_packaged_svelte_ui_works_under_path_prefix() -> None:
    subapp = create_agent_kit_app(registry=AgentRegistry())
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
    app = create_agent_kit_app(registry=AgentRegistry())
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
    app = create_agent_kit_app(registry=AgentRegistry())
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


async def test_workflow_execution_state_reports_running_workflow_open() -> None:
    handle = _FakeWorkflowHandle(status=WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(handle)

    result = await _workflow_execution_state(temporal, "agent-session-test")

    assert result == {
        "workflow_id": "agent-session-test",
        "execution_status": "RUNNING",
        "closed": False,
    }


def _relative_asset_paths(index_html: str) -> set[str]:
    return set(re.findall(r'(?:src|href)="\./([^"]+)"', index_html))


class _FakeWorkflowHandle:
    def __init__(self, *, status: WorkflowExecutionStatus) -> None:
        self.status = status

    async def describe(self):
        return SimpleNamespace(status=self.status)


class _FakeTemporalClient:
    def __init__(self, handle: _FakeWorkflowHandle) -> None:
        self.handle = handle

    def get_workflow_handle(self, _workflow_id: str) -> _FakeWorkflowHandle:
        return self.handle
