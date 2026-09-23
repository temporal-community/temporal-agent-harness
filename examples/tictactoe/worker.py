"""Worker for the tic-tac-toe agent.

Run from the repo root with:
    uv run --group examples python -m examples.tictactoe.worker

(or `just worker` from examples/tictactoe.)

Hosts the TicTacToeAgent workflow. Its TypeSafe tool (`typesafe_system_one`) is an
activity-backed tool whose durable body `AgentHarnessPlugin(tools=...)` registers; `place_mark`
is an inline workflow tool with no worker-side body.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    TYPESAFE_API_KEY                          required — the agent calls the TypeSafe API
                                              (TYPESAFE_AI_API_KEY is accepted as an alias)
    TYPESAFE_DEFAULT_MODEL                    optional SDK default (the workflow requests jev-latest)
    TICTACTOE_AGENT_TASK_QUEUE                task queue to poll (default: tictactoe-agent)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.plugin import AgentHarnessPlugin

from . import activities
from .workflow import TASK_QUEUE, TicTacToeAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("TICTACTOE_AGENT_TASK_QUEUE", TASK_QUEUE)

    # Checked up front so a missing key fails at startup, not on the first turn.
    if not activities.typesafe_api_key():
        sys.exit("error: TYPESAFE_API_KEY env var not set")

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config, plugins=[AgentHarnessPlugin(tools=activities.ALL_TOOLS)]
    )

    worker = Worker(client, task_queue=task_queue, workflows=[TicTacToeAgentWorkflow])
    print(
        f"Tic-tac-toe agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
