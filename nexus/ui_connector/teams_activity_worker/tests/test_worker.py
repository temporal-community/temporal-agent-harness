import asyncio
import runpy
from importlib.metadata import entry_points
from typing import Any

import pytest

from teams_activity_worker.contracts import TextMetadata
from teams_activity_worker.worker import Settings, _platform_from_settings, _worker_task_queue, main


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


@pytest.mark.asyncio
async def test_sdk_app_initializes_for_proactive_messaging() -> None:
    platform = _platform_from_settings(
        Settings(
            microsoft_tenant_id="tenant",
            microsoft_app_id="app",
            microsoft_app_password="secret",
        ),
        "teams-worker-1",
    )

    assert platform.app is not None
    await platform.app.initialize()
    activity_operations = platform._activities(
        TextMetadata(
            sender_id="user-1",
            session_id="teams:conversation-1",
            thread_id="",
            text="",
            service_url="https://tenant.test/teams/",
            channel_id="msteams",
        )
    )
    assert callable(activity_operations.create)
    assert callable(activity_operations.reply)
    assert callable(activity_operations.update)
    await platform.app.stop()


def test_worker_task_queues_are_unique_per_process() -> None:
    first = _worker_task_queue("nexus-connector-teams")
    second = _worker_task_queue("nexus-connector-teams")

    assert first.startswith("nexus-connector-teams-stream-")
    assert second.startswith("nexus-connector-teams-stream-")
    assert first != second
