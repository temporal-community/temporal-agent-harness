"""Worker helpers for the packaged harness web/session-manager layer."""

from __future__ import annotations

from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from nexus_a2a import NexusA2AServiceHandler, make_agent_card
from temporal_agent_harness.a2a.adapter import (
    HarnessA2ABackend,
    HarnessA2ABackendConfig,
)
from temporal_agent_harness.a2a.control_handler import (
    HarnessControlConfig,
    HarnessControlServiceHandler,
)
from temporal_agent_harness.web.session_manager import (
    SESSION_MANAGER_TASK_QUEUE,
    SessionManagerWorkflow,
)

_OWNED_WORKER_KWARGS = {"activities", "workflows"}


def create_session_manager_worker(
    client: Client,
    *,
    task_queue: str = SESSION_MANAGER_TASK_QUEUE,
    nexus_endpoint: str | None = None,
    **worker_kwargs: Any,
) -> Worker:
    """Create a Temporal worker that hosts the packaged session-manager workflow.

    The caller owns connecting the Temporal client and running the returned worker.
    Operational ``Worker`` settings such as identity, interceptors, and tuning options
    can be passed through as keyword arguments. Set ``nexus_endpoint`` to opt into the
    A2A and harness-control Nexus front door used by the UI tunnel. The default keeps
    the original direct UI path and registers no Nexus handlers.
    """

    owned_worker_kwargs = set(_OWNED_WORKER_KWARGS)
    if nexus_endpoint is not None:
        owned_worker_kwargs.add("nexus_service_handlers")
    conflicting = sorted(owned_worker_kwargs.intersection(worker_kwargs))
    if conflicting:
        raise ValueError(
            "create_session_manager_worker owns these Worker argument(s): "
            f"{', '.join(conflicting)}"
        )

    nexus_service_handlers = None
    if nexus_endpoint is not None:
        nexus_service_handlers = [
            NexusA2AServiceHandler(
                HarnessA2ABackend(
                    client,
                    HarnessA2ABackendConfig(
                        agent_task_queue="",
                        workflow_name="",
                        workflow_id_prefix="",
                        is_message_queuing_enabled=True,
                        agent_card=make_agent_card(
                            name="Temporal Agent Harness",
                            description="An agent session selected through the harness UI.",
                            endpoint=nexus_endpoint,
                        ),
                        start_missing_tasks=False,
                    ),
                )
            ),
            HarnessControlServiceHandler(client, HarnessControlConfig()),
        ]

    worker_options = dict(
        task_queue=task_queue,
        workflows=[SessionManagerWorkflow],
        **worker_kwargs,
    )
    if nexus_service_handlers is not None:
        worker_options["nexus_service_handlers"] = nexus_service_handlers
    return Worker(client, **worker_options)
