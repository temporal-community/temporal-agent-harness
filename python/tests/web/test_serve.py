"""``run_server``: the process around the web app that ``temporal-agent-harness serve`` runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from temporal_agent_harness.web import serve


@pytest.fixture
def served_apps(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Stand in for the app, Temporal and uvicorn; collects each app uvicorn was given."""
    served: list[object] = []

    async def reachable() -> None:
        return None

    monkeypatch.setattr(serve, "create_app", lambda *paths: object())
    monkeypatch.setattr(serve, "verify_temporal_reachable", reachable)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: served.append(app))
    return served


def test_serve_without_a_console_says_how_to_get_one_and_still_serves(
    served_apps: list[object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(serve, "packaged_ui_dist", lambda: None)

    serve.run_server("agents.toml")

    err = capsys.readouterr().err
    assert "No browser console" in err
    assert "PyPI" in err
    assert "bazel run //python/temporal_agent_harness/web:cli -- serve" in err
    assert len(served_apps) == 1


def test_serve_with_a_console_prints_nothing_about_it(
    served_apps: list[object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(serve, "packaged_ui_dist", lambda: tmp_path)

    serve.run_server("agents.toml")

    assert "console" not in capsys.readouterr().err
    assert len(served_apps) == 1
