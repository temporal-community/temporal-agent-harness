"""Worker for the agent registry (NEXUS_SUBAGENT=1 opt-in feature).

Its own standalone process, distinct from any agent's own worker — the registry is a shared
directory any agent can register with and any parent can discover from, not tied to one agent.

Run with (from anywhere agent-registry is installed, e.g. this repo's root venv during dev, or
a consumer project that depends on it via git+subdirectory=nexus/agent_registry):
    uv run python -m agent_registry.run_worker

Prerequisite (one-time, per environment): register a Nexus endpoint pointing at this worker's
task queue, e.g.:
    temporal operator nexus endpoint create \\
        --name agent-registry-endpoint \\
        --target-namespace default \\
        --target-task-queue agent-registry

Connection settings come from a ``temporal.toml`` profile, resolved through temporalio's
``ClientConfig.load_client_connect_config()`` — same convention as examples/monty/worker.py.

Env vars:
    TEMPORAL_CONFIG_FILE   path to a temporal.toml
    TEMPORAL_PROFILE       profile name to load (default: "default")
    AGENT_REGISTRY_TASK_QUEUE   task queue to poll (default: agent-registry)
"""

from __future__ import annotations

from typing import Any

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from agent_registry.registry_service_handler import (
    AgentRegistryServiceHandler,
)
from agent_registry.registry_workflow import (
    AGENT_REGISTRY_WORKFLOW_ID,
    AgentRegistryWorkflow,
)


AGENT_REGISTRY_TASK_QUEUE = "agent-registry"

_OWNED_WORKER_KWARGS = {"workflows", "nexus_service_handlers"}


async def ensure_agent_registry_workflow_started(
    client: Client, *, task_queue: str = AGENT_REGISTRY_TASK_QUEUE
) -> None:
    """Idempotently start the singleton ``AgentRegistryWorkflow``. Safe to call every time a
    registry worker boots — ``USE_EXISTING`` makes this a no-op once it's already running."""
    await client.start_workflow(
        AgentRegistryWorkflow.run,
        id=AGENT_REGISTRY_WORKFLOW_ID,
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


def create_agent_registry_worker(
    client: Client,
    *,
    task_queue: str = AGENT_REGISTRY_TASK_QUEUE,
    **worker_kwargs: Any,
) -> Worker:
    """Build the Temporal worker that hosts ``AgentRegistryWorkflow`` and its Nexus service
    handler. The caller owns connecting the client, calling
    ``ensure_agent_registry_workflow_started`` once, and running the returned worker."""
    conflicting = sorted(_OWNED_WORKER_KWARGS.intersection(worker_kwargs))
    if conflicting:
        raise ValueError(
            "create_agent_registry_worker owns these Worker argument(s): "
            f"{', '.join(conflicting)}"
        )
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AgentRegistryWorkflow],
        nexus_service_handlers=[AgentRegistryServiceHandler(client)],
        **worker_kwargs,
    )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    task_queue = os.environ.get("AGENT_REGISTRY_TASK_QUEUE", AGENT_REGISTRY_TASK_QUEUE)

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=pydantic_data_converter,
    )

    await ensure_agent_registry_workflow_started(client, task_queue=task_queue)

    worker: Worker = create_agent_registry_worker(client, task_queue=task_queue)
    print(
        f"Agent registry worker ready: profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
