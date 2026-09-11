"""The session-manager worker: both the factory and the process that runs it.

Named for what it hosts. ``session_manager.py`` next door defines the workflow and the registry
types; this module is the worker that hosts that workflow, and nothing else — it registers no
agent workflows or activities of its own.

``create_session_manager_worker`` is the library seam — you own the client and the run loop, and
can fold the session manager into a worker process you already have. ``run_session_manager_worker``
is the process layer on top: connect, run, report, stop cleanly. The ``session-manager``
subcommand of the ``temporal-agent-harness`` console script is that second function, so nobody
has to hand-write a launcher to get a working harness.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from temporal_agent_harness.web.session_manager import (
    SESSION_MANAGER_TASK_QUEUE,
    SessionManagerWorkflow,
)

_OWNED_WORKER_KWARGS = {"activities", "workflows"}


def create_session_manager_worker(
    client: Client,
    *,
    task_queue: str = SESSION_MANAGER_TASK_QUEUE,
    **worker_kwargs: Any,
) -> Worker:
    """Create a Temporal worker that hosts the packaged session-manager workflow.

    The caller owns connecting the Temporal client and running the returned worker.
    Operational ``Worker`` settings such as identity, interceptors, and tuning options
    can be passed through as keyword arguments. The helper intentionally owns the
    workflow/activity registration so the worker remains agent-agnostic.
    """

    conflicting = sorted(_OWNED_WORKER_KWARGS.intersection(worker_kwargs))
    if conflicting:
        raise ValueError(
            "create_session_manager_worker owns these Worker argument(s): "
            f"{', '.join(conflicting)}"
        )

    return Worker(
        client,
        task_queue=task_queue,
        workflows=[SessionManagerWorkflow],
        **worker_kwargs,
    )


async def _run_session_manager_worker(*, task_queue: str, identity: str | None) -> None:
    from temporal_agent_harness.web.client import connect_client

    client = await connect_client()
    # `identity` is omitted rather than passed as None so the SDK applies its own default.
    worker_kwargs = {} if identity is None else {"identity": identity}
    worker = create_session_manager_worker(client, task_queue=task_queue, **worker_kwargs)
    print(
        f"Session manager worker ready: taskQueue={task_queue!r} "
        f"namespace={client.namespace!r}",
        flush=True,
    )
    await worker.run()


def run_session_manager_worker(
    *,
    task_queue: str = SESSION_MANAGER_TASK_QUEUE,
    identity: str | None = None,
) -> None:
    """Connect to Temporal and run the session-manager worker until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    try:
        asyncio.run(_run_session_manager_worker(task_queue=task_queue, identity=identity))
    except KeyboardInterrupt:
        # Ctrl-C on a long-running worker is a normal stop, not a crash to traceback about.
        pass
