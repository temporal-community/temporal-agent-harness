"""Worker that hosts the agent-agnostic SessionManagerWorkflow.

Run from the repo root with:
    uv run python -m examples.session_manager.worker

The session manager is shared infrastructure, not an agent: it owns session lifecycle
and launches any registered agent as a child workflow on that agent's OWN task queue (from
``agents.toml``). So it runs on its own queue (``SESSION_MANAGER_TASK_QUEUE``), separate
from any agent worker, and registers only the manager workflow — no agent workflows, no
activities. The FastAPI server (``app.py``) starts the manager (passing the registry as its
init arg) and reconnects to it by ``SESSION_MANAGER_ID``; this worker is what actually runs it.

To drive the Monty demos end-to-end you therefore run three processes: this session-manager
worker, the Monty agent worker (``examples/monty/worker.py``), and the server (``app.py``).

Connection settings come from a ``temporal.toml`` profile, resolved through temporalio's
``ClientConfig.load_client_connect_config()``, which reads TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE
from the environment (the examples set these in ``.env.local``).

Env vars:
    TEMPORAL_CONFIG_FILE         path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE             profile name to load (default: "default")
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .workflow import SESSION_MANAGER_TASK_QUEUE, SessionManagerWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    # Match the agent workers and the server: the large-payload offload codec. The manager's
    # own payloads (the registry, session lists) are small, but the converter must agree with
    # whatever clients query this workflow, and the server connects with this codec — so use
    # it here too rather than risk a converter mismatch.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    # Only the manager workflow runs here. It dispatches each agent session to that agent's
    # own queue (resolved from the registry), so this worker hosts no agent and no activities.
    worker = Worker(
        client,
        task_queue=SESSION_MANAGER_TASK_QUEUE,
        workflows=[SessionManagerWorkflow],
    )
    print(
        f"Session manager worker ready: taskQueue={SESSION_MANAGER_TASK_QUEUE!r} "
        f"namespace={connect_config.get('namespace')}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
