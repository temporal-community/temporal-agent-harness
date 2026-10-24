"""Worker for the swarm triage example.

Run from the repo root with:
    bazel run //python/examples/swarm_triage:worker

(or `just worker` from examples/swarm_triage.)

Hosts all five agents on one queue. No model is called, so no API key is needed. The harness
plugin brings everything the swarm uses besides the agents: the subagent and peer turn
activities, the board activity, and the board workflow itself.

A run keeps hundreds of agents alive at once, and every one of them holds an activity open while
it waits on another (a lead on each worker turn, a worker on each peer turn), so the worker allows
far more concurrent activities than the SDK default. Most of them are idle long-polls.

The namespace must have the swarm search attributes registered; the root `just temporal` recipe
starts a dev server with them.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    SWARM_TRIAGE_TASK_QUEUE                   task queue to poll (default: swarm-triage)
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.swarm_triage.triage_agents import ALL_AGENTS, TASK_QUEUE


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    task_queue = os.environ.get("SWARM_TRIAGE_TASK_QUEUE", TASK_QUEUE)
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[AgentHarnessPlugin()])
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=ALL_AGENTS,
        max_concurrent_activities=2000,
        max_concurrent_workflow_tasks=200,
    )
    print(
        f"Swarm triage worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
