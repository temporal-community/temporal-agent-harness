import asyncio
import runpy
from typing import Any

import pytest


def test_module_execution_runs_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def fake_run(coroutine: Any) -> None:
        calls.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(asyncio, "run", fake_run)
    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        runpy.run_module("teams_activity_worker.worker", run_name="__main__")

    assert len(calls) == 1
