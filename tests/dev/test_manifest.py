"""harness.toml: loading, the agent registry it assembles, and the environment it builds."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from temporal_agent_harness.dev.manifest import load_manifest, parse_env_file

INCLUDED = """
[[agents]]
key = "included"
workflow_type = "IncludedAgent"
task_queue = "q1"
label = "Included"
description = "From an agents.toml."
"""

MANIFEST = """
[project]
name = "demo"
env_file = ".env.local"
registries = ["sub/agents.toml"]

[[agents]]
key = "inline"
workflow_type = "InlineAgent"
task_queue = "q2"
label = "Inline"
description = "Declared in harness.toml."

[[workers]]
name = "inline"
command = ["python", "-m", "demo.worker"]
requires = ["DEMO_KEY"]
watch = ["demo"]
"""


def _project(tmp_path: Path, manifest: str = MANIFEST) -> Path:
    (tmp_path / "sub").mkdir(exist_ok=True)
    (tmp_path / "sub" / "agents.toml").write_text(INCLUDED)
    path = tmp_path / "harness.toml"
    path.write_text(manifest)
    return path


def test_the_registry_merges_included_files_and_inline_agents(tmp_path: Path):
    manifest = load_manifest(_project(tmp_path))
    assert [a.key for a in manifest.registry().agents] == ["included", "inline"]


def test_a_manifest_without_inline_agents_is_not_itself_a_registry(tmp_path: Path):
    path = _project(tmp_path, MANIFEST.split("[[agents]]")[0])
    assert load_manifest(path).registry_paths() == [(tmp_path / "sub" / "agents.toml").resolve()]


def test_the_env_file_fills_in_but_never_overrides_the_real_environment(tmp_path: Path):
    manifest = load_manifest(_project(tmp_path))
    (tmp_path / ".env.local").write_text('DEMO_KEY="from-file"\nOTHER=1 # comment\n')
    env = manifest.environment({"OTHER": "exported"})
    assert env["DEMO_KEY"] == "from-file"
    assert env["OTHER"] == "exported"


def test_a_worker_missing_a_required_variable_is_reported(tmp_path: Path):
    manifest = load_manifest(_project(tmp_path))
    worker = manifest.workers[0]
    assert manifest.missing_requirements(worker, {}) == ["DEMO_KEY"]
    assert manifest.missing_requirements(worker, {"DEMO_KEY": "x"}) == []


def test_mistakes_are_caught_when_loading(tmp_path: Path):
    with pytest.raises(ValidationError):
        load_manifest(_project(tmp_path, MANIFEST + '\n[[workers]]\nname = "no command"\ncommand = []\n'))
    dup = MANIFEST + '\n[[workers]]\nname = "inline"\ncommand = ["x"]\n'
    with pytest.raises(ValueError, match="duplicate worker"):
        load_manifest(_project(tmp_path, dup))
    with pytest.raises(ValidationError):
        load_manifest(_project(tmp_path, MANIFEST + "\n[dev]\nunknown_setting = 1\n"))


def test_env_file_parsing():
    assert parse_env_file(
        "# comment\n\nexport A=1\nB='two words'\nC=\"quoted # not a comment\"\nD=x # trailing\nE=\n"
    ) == {"A": "1", "B": "two words", "C": "quoted # not a comment", "D": "x", "E": ""}


def test_this_repositorys_manifest_loads():
    repo = Path(__file__).resolve().parents[2]
    manifest = load_manifest(repo / "harness.toml")
    assert {"openai-hello", "monty-chat", "tictactoe"} <= {
        a.key for a in manifest.registry().agents
    }
    for worker in manifest.workers:
        for path in worker.watch:
            assert manifest.resolve(path).exists(), f"{worker.name} watches missing {path}"


def test_serve_accepts_a_manifest(tmp_path: Path, monkeypatch):
    from temporal_agent_harness.web import cli

    served: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        "temporal_agent_harness.web.serve.run_server", lambda *paths, **_: served.append(paths)
    )
    cli.main(["serve", "--manifest", str(_project(tmp_path))])
    assert served == [
        (str((tmp_path / "sub" / "agents.toml").resolve()), str((tmp_path / "harness.toml").resolve()))
    ]
