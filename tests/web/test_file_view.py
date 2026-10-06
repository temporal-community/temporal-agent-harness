# ABOUTME: Tests for the console's file view: POST /api/files/view runs a mount's
# vfs.<name>.view activity as a standalone activity on a real local dev server (the time-skipping
# test server cannot run standalone activities), pages through the file, and turns failures
# into HTTP errors the viewer can show.
#
# Run with: uv run pytest tests/web/test_file_view.py -v

from __future__ import annotations

import errno
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import PurePosixPath

import httpx
import pytest
import pytest_asyncio
from pydantic import BaseModel
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.code_mode.activity_fs import MAX_VIEW_BYTES
from temporal_agent_harness.web import file_view
from temporal_agent_harness.web.app import create_agent_harness_app
from temporal_agent_harness.web.session_manager import AgentRegistry

FILES = {"notes.md": b"# Notes\n", "big.txt": b"y" * (MAX_VIEW_BYTES + 3)}


class PagesConfig(BaseModel):
    shelf: str


class Pages(agent.ActivityFileSystem[PagesConfig], name="web-view-test"):
    def __init__(self, config: PagesConfig) -> None:
        if config.shelf != "main":
            raise ValueError(f"no shelf {config.shelf!r}")

    async def stat(self, path: PurePosixPath) -> agent.FileStat | None:
        if str(path) == ".":
            return agent.FileStat(is_dir=True)
        data = FILES.get(str(path))
        return None if data is None else agent.FileStat(is_dir=False, size=len(data), mtime=7.0)

    async def read(
        self, path: PurePosixPath, offset: int = 0, length: int | None = None
    ) -> bytes:
        data = FILES.get(str(path))
        if data is None:
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))
        return data[offset : None if length is None else offset + length]

    async def list(self, path: PurePosixPath) -> list[str]:
        return sorted(FILES)


@pytest_asyncio.fixture(scope="module")
async def env() -> AsyncGenerator[WorkflowEnvironment, None]:
    env = await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter)
    yield env
    await env.shutdown()


@pytest_asyncio.fixture
async def task_queue(env: WorkflowEnvironment) -> AsyncGenerator[str, None]:
    queue = f"file-view-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=queue, activities=Pages.activities):
        yield queue


def _body(task_queue: str, path: str, *, offset: int = 0, shelf: str = "main") -> dict:
    return {
        "source": {
            "filesystem": "web-view-test",
            "config": {"shelf": shelf},
            "task_queue": task_queue,
        },
        "path": path,
        "offset": offset,
    }


async def _post(env: WorkflowEnvironment, body: dict) -> httpx.Response:
    app = create_agent_harness_app(registry=AgentRegistry(agents=[]))
    app.state.temporal = env.client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        return await http.post("/api/files/view", json=body)


async def test_a_file_is_read_by_a_standalone_activity(env, task_queue):
    response = await _post(env, _body(task_queue, "notes.md"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "content": "# Notes\n",
        "encoding": "utf-8",
        "offset": 0,
        "size": 8,
        "mtime": 7.0,
    }
    assert response.headers["cache-control"] == "no-store"


async def test_a_large_file_comes_back_a_page_at_a_time(env, task_queue):
    first = (await _post(env, _body(task_queue, "big.txt"))).json()
    assert (len(first["content"]), first["size"]) == (MAX_VIEW_BYTES, MAX_VIEW_BYTES + 3)
    rest = (await _post(env, _body(task_queue, "big.txt", offset=MAX_VIEW_BYTES))).json()
    assert (rest["offset"], rest["content"]) == (MAX_VIEW_BYTES, "yyy")


@pytest.mark.parametrize(
    ("path", "shelf", "status", "error"),
    [
        ("missing.md", "main", 404, "FileNotFoundError"),
        (".", "main", 400, "IsADirectoryError"),
        ("notes.md", "attic", 400, "ValueError"),  # the filesystem refused the config
    ],
)
async def test_failures_come_back_as_http_errors(env, task_queue, path, shelf, status, error):
    response = await _post(env, _body(task_queue, path, shelf=shelf))
    assert response.status_code == status, response.text
    assert response.json()["error"] == error


async def test_a_request_is_checked_before_anything_runs(env, task_queue):
    body = _body(task_queue, "notes.md")
    body["source"]["filesystem"] = "Not.A.Name"
    response = await _post(env, body)
    assert (response.status_code, response.json()["error"]) == (400, "invalid_filesystem")
    body = _body(task_queue, "notes.md")
    body["source"]["config"] = ["not", "an", "object"]
    response = await _post(env, body)
    assert response.status_code == 422


async def test_a_queue_with_no_worker_times_out(env, monkeypatch):
    monkeypatch.setattr(file_view, "_TIMEOUT", timedelta(seconds=2))
    response = await _post(env, _body(f"nobody-{uuid.uuid4()}", "notes.md"))
    assert (response.status_code, response.json()["error"]) == (504, "no_worker")


async def test_a_server_without_standalone_activities_says_so():
    test_server = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    try:
        response = await _post(test_server, _body("any-queue", "notes.md"))
    finally:
        await test_server.shutdown()
    assert (response.status_code, response.json()["error"]) == (501, "file_view_unavailable")
