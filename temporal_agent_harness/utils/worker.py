"""ABOUTME: Runs a Temporal worker so its "ready" banner is only printed once the worker has
actually reached its server, rather than before the first connection attempt.

``Worker.run()`` validates eagerly — it describes the configured namespace on the server it
connected to, and only then sets the worker running (``Worker.is_running``). Printing the banner
before that call, which is what every worker module used to do, announces a readiness nothing has
established yet: a worker aimed at an address or namespace that doesn't exist prints "ready" and
then dies, and "ready" is the line people look for. Waiting on the SDK's own signal costs one
await and needs no health-check of our own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from temporalio.worker import Worker


def _print(ready: str) -> None:
    print(ready, flush=True)


async def run_worker(
    worker: Worker,
    ready: str,
    announce: Callable[[str], None] = _print,
) -> None:
    """Run ``worker`` to completion, announcing ``ready`` once it has validated and started.

    ``announce`` defaults to printing; pass ``logger.info`` for workers that log their banner.

    Raises whatever ``Worker.run()`` raises. A worker that cannot reach its server, or whose
    namespace is not on the server it reached, therefore fails with that error and never claims
    to be ready.
    """
    running = asyncio.create_task(worker.run())
    # ponytail: polled rather than event-driven because the SDK exposes readiness only as the
    # is_running property. If it ever grows a "started" hook or event, await that instead.
    while not worker.is_running:
        if running.done():
            # Validation failed (or the worker stopped early): re-raise instead of announcing.
            await running
            return
        await asyncio.sleep(0.05)
    announce(ready)
    await running
