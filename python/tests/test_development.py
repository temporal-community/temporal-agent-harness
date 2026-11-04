"""Installed-resource updates must preserve user work and make conflicts actionable."""
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from temporal_agent_harness.development import (
    api_checks,
    create_starter,
    install_skill,
    project_name,
    skill_destinations,
)
from temporal_agent_harness.web.cli import main


def bundle(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("first")
    (source / "old.md").write_text("owned")
    return source


def test_skill_update_preserves_edits_and_does_not_partially_write(tmp_path):
    source = bundle(tmp_path)
    root, changed = install_skill(tmp_path / "skills", source=source, sdk_version="1")
    assert changed
    assert install_skill(tmp_path / "skills", source=source, sdk_version="1").changed is False
    (root / "old.md").write_text("my edit")
    (source / "SKILL.md").write_text("second")
    with pytest.raises(ValueError, match="Local skill edits preserved"):
        install_skill(tmp_path / "skills", source=source, sdk_version="2")
    assert (root / "SKILL.md").read_text() == "first"
    assert (root / "old.md").read_text() == "my edit"
    (root / "old.md").write_text("owned")
    (source / "old.md").unlink()
    (root / "custom.md").write_text("mine")
    assert install_skill(tmp_path / "skills", source=source, sdk_version="2").changed
    assert (root / "SKILL.md").read_text() == "second"
    assert not (root / "old.md").exists()
    assert (root / "custom.md").read_text() == "mine"
    assert json.loads((root / ".harness-skill.json").read_text())["version"] == "2"


def test_skill_update_rejects_symlinks_and_manifest_traversal(tmp_path):
    source = bundle(tmp_path)
    root = install_skill(tmp_path / "skills", source=source, sdk_version="1").root
    outside = tmp_path / "outside"
    outside.write_text("leave alone")
    (root / "old.md").unlink()
    (root / "old.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        install_skill(tmp_path / "skills", source=source, sdk_version="2")
    assert outside.read_text() == "leave alone"
    (root / "old.md").unlink()
    (root / ".harness-skill.json").write_text(json.dumps({"name":"temporal-harness-python", "files":{"../../outside":"x"}}))
    with pytest.raises(ValueError, match="Invalid resource path"):
        install_skill(tmp_path / "skills", source=source, sdk_version="2")


def test_skill_destinations_map_tools_to_their_directories():
    assert skill_destinations(None, None) == [Path(".agents/skills")]
    assert skill_destinations(["claude", "codex", "claude"], None) == [Path(".claude/skills"), Path(".agents/skills")]
    assert skill_destinations(["claude"], "tools/skills") == [Path(".claude/skills"), Path("tools/skills")]
    with pytest.raises(ValueError, match="Unknown coding agent cursor"):
        skill_destinations(["cursor"], None)


def test_skills_install_for_claude_reports_its_location(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("temporal_agent_harness.development.package_version", lambda: "9.9.9")
    main(["skills", "install", "--for", "claude"])
    out = capsys.readouterr().out
    assert (tmp_path / ".claude/skills/temporal-harness-python/SKILL.md").is_file()
    assert not (tmp_path / ".agents").exists()
    assert out.startswith("Installed temporal-harness-python 9.9.9 in ")
    assert "/temporal-harness-python" in out
    main(["skills", "install", "--for", "claude"])
    assert capsys.readouterr().out.startswith("Already up to date: temporal-harness-python ")


def test_project_name_comes_from_the_directory():
    assert project_name(Path("/tmp/My Agent.Project")) == "my-agent-project"
    assert project_name(Path("/tmp/ ")) == "my-agent" or project_name(Path("/tmp/-")) == "my-agent"


def test_starter_pins_the_install_and_refuses_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml.tmpl").write_text('name = "__PROJECT_NAME__"\nversion = "__HARNESS_VERSION__"')
    (source / "gitignore.tmpl").write_text(".env.local\n")
    root = create_starter(tmp_path / "app", source=source, sdk_version="0.9.0")
    assert '"0.9.0"' in (root / "pyproject.toml").read_text()
    assert 'name = "app"' in (root / "pyproject.toml").read_text()
    assert (root / ".gitignore").is_file()
    with pytest.raises(ValueError, match="overwrite"):
        create_starter(root, source=source, sdk_version="1.0.0")
    assert '"0.9.0"' in (root / "pyproject.toml").read_text()


def test_api_diagnostics_fail_closed_without_leaking_response(capsys):
    with patch("temporal_agent_harness.development.urlopen", side_effect=OSError("secret")):
        report = api_checks("http://localhost:8000", .01)
    assert report[0]["ok"] is False
    assert "secret" not in json.dumps(report)


def test_openapi_generation_needs_no_running_services(capsys):
    main(["schema", "--openapi"])
    schema = json.loads(capsys.readouterr().out)
    assert "/api/messages" in schema["paths"]
    assert "/api/attach" in schema["paths"]


def test_doctor_reports_invalid_registry_and_unavailable_services(tmp_path, monkeypatch, capsys):
    from temporal_agent_harness.web import doctor

    async def unavailable(queues, timeout):
        assert "session-manager" in queues[0]
        return [{"check": "temporal", "ok": False, "detail": "unavailable"}]

    monkeypatch.setattr(doctor, "inspect_temporal", unavailable)
    monkeypatch.setattr(doctor, "api_checks", lambda *args: [{"check": "api", "ok": False, "detail": "offline"}])
    with pytest.raises(SystemExit) as stopped:
        main(["doctor", "--registry", str(tmp_path / "missing.toml"), "--json"])
    assert stopped.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert [c["check"] for c in report["checks"]] == ["registry", "temporal", "api"]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_doctor_rejects_invalid_timeouts(timeout):
    from temporal_agent_harness.web.doctor import diagnose

    with pytest.raises(ValueError, match="positive"):
        diagnose(None, "http://localhost:8000", timeout)


def test_api_reports_worker_readiness_and_malformed_responses():
    from io import BytesIO

    def response(value):
        return BytesIO(json.dumps(value).encode())

    with patch("temporal_agent_harness.development.urlopen", return_value=response({
        "agents": [{"workflow_type": "Hello", "worker": {"status": "no_worker"}}]
    })):
        checks = api_checks("http://localhost:8000")
    assert checks[0]["ok"] is True
    assert checks[1] == {"check": "worker:Hello", "ok": False, "detail": "no_worker"}
    with patch("temporal_agent_harness.development.urlopen", return_value=response({"agents": [None]})):
        assert api_checks("http://localhost:8000")[-1]["ok"] is False


def test_dev_runs_the_session_manager_and_server_together(monkeypatch, tmp_path):
    from temporal_agent_harness.web import dev

    registry = tmp_path / "agents.toml"
    registry.write_text('[[agents]]\nkey = "a"\nworkflow_type = "A"\ntask_queue = "q"\nlabel = "A"\ndescription = "d"\n')
    calls: list[str] = []

    class FakeWorker:
        async def run(self):
            calls.append("worker")
            await asyncio.Event().wait()

        async def shutdown(self):
            calls.append("worker shutdown")

    class FakeServer:
        should_exit = False

        def __init__(self, config):
            calls.append(f"server:{config.port}")

        async def serve(self):
            calls.append("serve")

    class FakeClient:
        namespace = "default"

    async def verify():
        calls.append("verify")

    async def connect():
        return FakeClient()

    monkeypatch.setitem(sys.modules, "uvicorn", type("U", (), {"Server": FakeServer, "Config": lambda app, host, port: type("C", (), {"port": port})()}))
    monkeypatch.setattr(dev, "verify_temporal_reachable", verify)
    monkeypatch.setattr(dev, "connect_client", connect)
    monkeypatch.setattr(dev, "create_app", lambda *paths: "app")
    monkeypatch.setattr(dev, "create_session_manager_worker", lambda client, task_queue: FakeWorker())
    monkeypatch.setattr(dev, "warn_without_console", lambda: None)
    main(["dev", str(registry), "--port", "9001"])
    assert calls[:3] == ["verify", "server:9001", "worker"] or calls[:3] == ["verify", "server:9001", "serve"]
    assert "worker shutdown" in calls and "serve" in calls
