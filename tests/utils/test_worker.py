"""ABOUTME: Covers the one thing run_worker exists to guarantee — that "ready" is never announced
by a worker that failed to reach its server.

The failure this guards against is a silent one in practice: the banner is the line people grep
for when checking a worker, so announcing it before Worker.run() has validated the connection and
namespace makes a misconfigured worker look healthy. A fake worker reproduces the SDK's ordering
(validate, then set is_running) without needing a server.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from temporalio.worker import Worker

from temporal_agent_harness.utils.worker import run_worker


class _FakeWorker:
    """Mimics ``Worker.run()``: validate first, only then report ``is_running``."""

    def __init__(self, *, fails_validation: bool) -> None:
        self._fails_validation = fails_validation
        self.is_running = False

    async def run(self) -> None:
        await asyncio.sleep(0)
        if self._fails_validation:
            raise RuntimeError(
                "Worker validation failed: Namespace nope was not found or otherwise "
                "could not be described"
            )
        self.is_running = True
        await asyncio.sleep(3600)


async def test_validation_failure_is_raised_and_nothing_is_announced() -> None:
    announced: list[str] = []

    with pytest.raises(RuntimeError, match="Worker validation failed"):
        await run_worker(
            cast(Worker, _FakeWorker(fails_validation=True)),
            "worker ready",
            announced.append,
        )

    assert announced == []


async def test_announces_once_the_worker_is_running() -> None:
    announced: list[str] = []
    running = asyncio.create_task(
        run_worker(
            cast(Worker, _FakeWorker(fails_validation=False)),
            "worker ready",
            announced.append,
        )
    )
    try:
        async with asyncio.timeout(5):
            while not announced:
                await asyncio.sleep(0.01)
    finally:
        running.cancel()

    assert announced == ["worker ready"]
