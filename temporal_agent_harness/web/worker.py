"""Worker helpers for the packaged harness web/session-manager layer."""

from __future__ import annotations

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
