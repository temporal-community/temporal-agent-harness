from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from temporal_agent_harness.ui import packaged_ui_dist
from temporal_agent_harness.web import (
    SESSION_MANAGER_TASK_QUEUE,
    AgentRegistry,
    SessionManagerWorkflow,
    create_agent_harness_app,
    create_session_manager_worker,
)

ROOT = Path(__file__).resolve().parents[2]
WHEEL_UI_PREFIX = "temporal_agent_harness/ui/dist/"


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


def test_just_server_app_serves_packaged_svelte_ui() -> None:
    from examples.session_manager.app import app

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
            "expected_turn": 1,
            "from_offset": 42,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any("from_offset" in item.get("loc", []) for item in detail)


def test_legacy_session_manager_static_html_entrypoints_are_removed() -> None:
    legacy_static = ROOT / "examples" / "session_manager" / "static"

    assert not list(legacy_static.glob("*.html"))


def test_create_session_manager_worker_registers_packaged_workflow() -> None:
    client = object()

    with patch("temporal_agent_harness.web.worker.Worker") as worker_cls:
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

    with patch("temporal_agent_harness.web.worker.Worker") as worker_cls:
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


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    import setuptools.build_meta as build_meta

    dist_root = tmp_path_factory.mktemp("dists")
    wheel_dir = dist_root / "wheel"
    sdist_dir = dist_root / "sdist"
    wheel_dir.mkdir()
    sdist_dir.mkdir()

    previous_cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        wheel_name = build_meta.build_wheel(str(wheel_dir))
        sdist_name = build_meta.build_sdist(str(sdist_dir))
    finally:
        os.chdir(previous_cwd)

    return wheel_dir / wheel_name, sdist_dir / sdist_name


def test_built_distributions_include_packaged_ui_assets(
    built_distributions: tuple[Path, Path],
) -> None:
    wheel_path, sdist_path = built_distributions

    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
        _assert_ui_assets_present(names)
        assert "temporal_agent_harness/web/worker.py" in names

        metadata = wheel.read("temporal_agent_harness-0.1.0.dist-info/METADATA").decode()
        assert "Provides-Extra: ui" in metadata
        assert 'Requires-Dist: fastapi[standard]>=0.136.3; extra == "ui"' in metadata

        html = wheel.read(f"{WHEEL_UI_PREFIX}index.html").decode()
        for asset_path in _relative_asset_paths(html):
            assert f"{WHEEL_UI_PREFIX}{asset_path}" in names

    with tarfile.open(sdist_path) as sdist:
        names = set(sdist.getnames())
        assert any(name.endswith("/temporal_agent_harness/web/worker.py") for name in names)
        assert any(name.endswith("/temporal_agent_harness/ui/dist/index.html") for name in names)
        assert any(name.endswith("/temporal_agent_harness/ui/dist/temporal-logo.svg") for name in names)
        assert any(
            name.endswith(".js") and "/temporal_agent_harness/ui/dist/assets/" in name
            for name in names
        )
        assert any(
            name.endswith(".css") and "/temporal_agent_harness/ui/dist/assets/" in name
            for name in names
        )


def test_extracted_wheel_can_resolve_packaged_ui_dist(
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    wheel_path, _ = built_distributions
    extracted = tmp_path / "wheel"
    extracted.mkdir()

    with zipfile.ZipFile(wheel_path) as wheel:
        wheel.extractall(extracted)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(extracted)
    script = """
from temporal_agent_harness.ui import packaged_ui_dist

dist = packaged_ui_dist()
assert dist is not None
assert (dist / "index.html").is_file()
assert (dist / "temporal-logo.svg").is_file()
print(dist)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _relative_asset_paths(index_html: str) -> set[str]:
    return set(re.findall(r'(?:src|href)="\./([^"]+)"', index_html))


def _assert_ui_assets_present(names: set[str]) -> None:
    assert f"{WHEEL_UI_PREFIX}index.html" in names
    assert f"{WHEEL_UI_PREFIX}temporal-logo.svg" in names
    assert any(
        name.startswith(f"{WHEEL_UI_PREFIX}assets/") and name.endswith(".js")
        for name in names
    )
    assert any(
        name.startswith(f"{WHEEL_UI_PREFIX}assets/") and name.endswith(".css")
        for name in names
    )
