import asyncio
import runpy
from importlib.metadata import entry_points
from typing import Any

import pytest

from teams_activity_worker.worker import main


def test_console_script_runs_worker_main() -> None:
    entry_point = next(item for item in entry_points(group="console_scripts") if item.name == "teams-activity-worker")

    assert entry_point.value == "teams_activity_worker.worker:main"
    assert entry_point.load() is main


def test_module_execution_runs_worker_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def fake_run(coroutine: Any) -> None:
        calls.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(asyncio, "run", fake_run)
    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        runpy.run_module("teams_activity_worker.worker", run_name="__main__")

    assert len(calls) == 1
