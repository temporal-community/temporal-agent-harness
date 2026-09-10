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
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.plugin import AgentHarnessPlugin
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

    # The session manager hosts no agent workflows and no tools, so what it needs from the
    # harness plugin is the shared data converter. That is the point — the manager, the web
    # server, and every agent worker read the same payloads because they all add the same
    # plugin. The harness activities come along too and are simply never called here (nothing
    # schedules a tool, a Code Mode step, or a subagent turn on this queue).
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[AgentHarnessPlugin()])

    worker = create_session_manager_worker(client)
    print(
        f"Session manager worker ready: taskQueue={SESSION_MANAGER_TASK_QUEUE!r} "
        f"namespace={connect_config.get('namespace')}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
