from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _show_recipe(name: str) -> str:
    return subprocess.check_output(
        ["just", "--show", name],
        cwd=ROOT,
        text=True,
    )


def test_root_aggregate_launch_excludes_the_chronicler_registry_and_worker() -> None:
    server = _show_recipe("server")
    workers = _show_recipe("workers")
    recipes = subprocess.check_output(
        ["just", "--summary"],
        cwd=ROOT,
        text=True,
    ).split()

    assert "examples/chronicler/agents.toml" not in server
    assert "worker-chronicler" not in recipes
    assert "just worker-chronicler &" not in workers
