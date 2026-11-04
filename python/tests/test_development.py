"""Installed-resource updates must preserve user work and make conflicts actionable."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from temporal_agent_harness.development import create_starter, install_skill, api_checks
from temporal_agent_harness.web.cli import main


def bundle(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("first")
    (source / "old.md").write_text("owned")
    return source


def test_skill_update_preserves_edits_and_does_not_partially_write(tmp_path):
    source = bundle(tmp_path)
    root = install_skill(tmp_path / "skills", source=source, sdk_version="1")
    (root / "old.md").write_text("my edit")
    (source / "SKILL.md").write_text("second")
    with pytest.raises(ValueError, match="Local skill edits preserved"):
        install_skill(tmp_path / "skills", source=source, sdk_version="2")
    assert (root / "SKILL.md").read_text() == "first"
    assert (root / "old.md").read_text() == "my edit"
    (root / "old.md").write_text("owned")
    (source / "old.md").unlink()
    (root / "custom.md").write_text("mine")
    install_skill(tmp_path / "skills", source=source, sdk_version="2")
    assert (root / "SKILL.md").read_text() == "second"
    assert not (root / "old.md").exists()
    assert (root / "custom.md").read_text() == "mine"
    assert json.loads((root / ".harness-skill.json").read_text())["version"] == "2"


def test_skill_update_rejects_symlinks_and_manifest_traversal(tmp_path):
    source = bundle(tmp_path)
    root = install_skill(tmp_path / "skills", source=source, sdk_version="1")
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


def test_starter_pins_the_install_and_refuses_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml.tmpl").write_text('version = "__HARNESS_VERSION__"')
    (source / "gitignore.tmpl").write_text(".env.local\n")
    root = create_starter(tmp_path / "app", source=source, sdk_version="0.9.0")
    assert '"0.9.0"' in (root / "pyproject.toml").read_text()
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
