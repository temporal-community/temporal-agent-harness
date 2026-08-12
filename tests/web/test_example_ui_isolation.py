from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from examples.chronicler.app import create_app as create_chronicler_app
from temporal_agent_harness.web import AgentRegistry


ROOT = Path(__file__).resolve().parents[2]


def test_chronicler_app_serves_its_injected_example_ui_assets(tmp_path: Path) -> None:
    example_ui = tmp_path / "ui_dist"
    example_ui.mkdir()
    (example_ui / "index.html").write_text("<title>Chronicler example UI</title>")

    app = create_chronicler_app(registry=AgentRegistry(), static_dir=example_ui)

    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert "Chronicler example UI" in response.text


def test_chronicler_app_requires_its_built_ui_bundle_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from examples.chronicler import app as chronicler_app

    monkeypatch.setattr(chronicler_app, "_UI_DIST", tmp_path / "missing-ui_dist")

    with pytest.raises(ValueError, match="Static UI directory does not exist"):
        create_chronicler_app(registry=AgentRegistry())


def test_chronicler_dev_builds_the_example_ui_bundle_before_starting() -> None:
    app_build = subprocess.check_output(
        ["just", "--show", "app-build"],
        cwd=ROOT / "examples" / "chronicler",
        text=True,
    )
    server = subprocess.check_output(
        ["just", "--show", "server"],
        cwd=ROOT / "examples" / "chronicler",
        text=True,
    )
    dev = subprocess.check_output(
        ["just", "--show", "dev"],
        cwd=ROOT / "examples" / "chronicler",
        text=True,
    )
    ui_dev = subprocess.check_output(
        ["just", "--show", "ui-dev"],
        cwd=ROOT / "examples" / "chronicler",
        text=True,
    )

    assert "server: app-build" in server
    assert "dev: app-build" in dev
    assert 'pnpm --dir "{{ example_ui }}" run build' in app_build
    assert 'pnpm --dir "{{ example_ui }}" run dev' in ui_dev


def test_chronicler_procfile_starts_the_app_factory() -> None:
    procfile = (ROOT / "examples" / "chronicler" / "Procfile").read_text()

    assert "uvicorn --factory examples.chronicler.app:create_app" in procfile
    assert "examples.chronicler.app:app" not in procfile
