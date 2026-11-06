"""One process for local development: the session-manager worker and the API/console server.

``serve`` and ``session-manager`` stay separate commands for deployments that scale or place
them independently. During development they are always started together against the same
Temporal namespace, so the ``dev`` subcommand of the ``temporal-agent-harness`` console script
runs both in one asyncio loop: Temporal, ``dev``, and your agent worker are the whole stack.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from temporal_agent_harness.utils.task_queue import task_queue_name
from temporal_agent_harness.web.client import connect_client, verify_temporal_reachable
from temporal_agent_harness.web.serve import create_app, warn_without_console
from temporal_agent_harness.web.session_manager import SESSION_MANAGER_TASK_QUEUE
from temporal_agent_harness.web.session_manager_worker import create_session_manager_worker


async def _run_dev(registry_paths: tuple[str | Path, ...], host: str, port: int) -> None:
    import uvicorn

    app = create_app(*registry_paths)
    warn_without_console()
    await verify_temporal_reachable()
    client = await connect_client()
    task_queue = task_queue_name(SESSION_MANAGER_TASK_QUEUE)
    worker = create_session_manager_worker(client, task_queue=task_queue)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port))
    print(
        f"Session manager worker ready: taskQueue={task_queue!r} namespace={client.namespace!r}",
        flush=True,
    )
    # Either side finishing (Ctrl-C reaches uvicorn's signal handler first) stops the other, so
    # the process never lingers as a half-stack.
    worker_task = asyncio.ensure_future(worker.run())
    server_task = asyncio.ensure_future(server.serve())
    done, pending = await asyncio.wait({worker_task, server_task}, return_when=asyncio.FIRST_COMPLETED)
    server.should_exit = True
    if worker_task in pending:
        await worker.shutdown()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()


def run_dev(*registry_paths: str | Path, host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the session-manager worker and the web app together until interrupted."""
    try:
        import uvicorn  # noqa: F401  # fail with the fix rather than a bare ModuleNotFoundError
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install shape
        raise SystemExit(
            "The harness web server needs the 'ui' extra: uv add 'temporal-agent-harness[ui]'"
        ) from exc
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    try:
        asyncio.run(_run_dev(registry_paths, host, port))
    except KeyboardInterrupt:
        pass
    print("Stopped the session manager and the API/console server.", file=sys.stderr)
