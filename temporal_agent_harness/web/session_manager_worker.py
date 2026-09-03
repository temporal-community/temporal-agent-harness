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
import os
from typing import Any

from nexus_a2a import NexusA2AServiceHandler, a2a_nexus_data_converter, make_agent_card
from temporalio.client import Client
from temporalio.worker import Worker

from temporal_agent_harness.a2a.adapter import (
    HarnessA2ABackend,
    HarnessA2ABackendConfig,
)
from temporal_agent_harness.a2a.control_handler import (
    HarnessControlConfig,
    HarnessControlServiceHandler,
)
from temporal_agent_harness.utils.large_payload import (
    DEFAULT_PAYLOAD_STORAGE,
    with_large_payload_offload,
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
    can be passed through as keyword arguments. The helper intentionally owns the
    workflow/activity registration so the worker remains agent-agnostic. Set
    ``nexus_endpoint`` to host the A2A and harness-control Nexus front door used by
    a UI tunnel. Omitting it preserves the direct, non-Nexus UI path.
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


async def _run_session_manager_worker(*, task_queue: str, identity: str | None) -> None:
    from temporal_agent_harness.web.client import connect_client

    nexus_endpoint = os.environ.get("NEXUS_UI_ENDPOINT", "").strip() or None
    data_converter = None
    if nexus_endpoint is not None:
        data_converter = with_large_payload_offload(
            a2a_nexus_data_converter,
            DEFAULT_PAYLOAD_STORAGE,
        )
    client = await connect_client(data_converter=data_converter)
    # `identity` is omitted rather than passed as None so the SDK applies its own default.
    worker_kwargs = {} if identity is None else {"identity": identity}
    worker = create_session_manager_worker(
        client,
        task_queue=task_queue,
        nexus_endpoint=nexus_endpoint,
        **worker_kwargs,
    )
    print(
        f"Session manager worker ready: taskQueue={task_queue!r} "
        f"namespace={client.namespace!r} "
        f"uiTransport={'nexus:' + nexus_endpoint if nexus_endpoint else 'direct'}",
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
        asyncio.run(
            _run_session_manager_worker(task_queue=task_queue, identity=identity)
        )
    except KeyboardInterrupt:
        # Ctrl-C on a long-running worker is a normal stop, not a crash to traceback about.
        pass
