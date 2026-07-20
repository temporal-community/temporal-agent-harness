"""Shared session-manager worker for the bundled examples.

The packaged ``SessionManagerWorkflow`` is agent-agnostic — it launches whichever agents the
example's server registered (from that example's ``agents.toml``) as child workflows on their own
task queues — so one worker serves every example. Each example's justfile ``session-manager``
recipe runs this same module:

    python -m examples.session_manager_worker

It runs on the packaged ``SESSION_MANAGER_TASK_QUEUE`` and registers no agent workflows or
activities of its own.
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.utils.large_payload import with_large_payload_offload
from temporal_agent_harness.web import (
    SESSION_MANAGER_TASK_QUEUE,
    create_session_manager_worker,
)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    worker = create_session_manager_worker(client)
    print(
        f"Session manager worker ready: taskQueue={SESSION_MANAGER_TASK_QUEUE!r} "
        f"namespace={connect_config.get('namespace')}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
