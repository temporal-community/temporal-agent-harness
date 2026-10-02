"""Worker for the agent DAG example.

Run from the repo root with:
    uv run --group examples python -m examples.agent_dag.worker

(or `just worker` from examples/agent_dag.)

Hosts both agents on one queue: DagBuilderAgent, and the DagStepAgent subagents its flows start.
The step tools' durable bodies (the Monty example's travel activities) are registered through
`AgentHarnessPlugin(tools=...)`, which also brings the subagent-turn activity. Code Mode itself
needs no activities: the workflow steps the sandbox, so this worker only needs the `code-mode`
extra installed. The editor tools are callback tools and `get_weather` is inline, so
neither needs a worker-side body.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — both agents call the OpenAI API
    AGENT_DAG_TASK_QUEUE                      task queue to poll (default: agent-dag)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .step_agent import TASK_QUEUE, DagStepAgentWorkflow
from .step_tools import TRAVEL_TOOLS
from .workflow import DagBuilderAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("AGENT_DAG_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    openai_plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(minutes=3),
            heartbeat_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )

    # Harness plugin LAST, so its large-payload converter wraps the OpenAI plugin's.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[openai_plugin, AgentHarnessPlugin(tools=TRAVEL_TOOLS)],
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[DagBuilderAgentWorkflow, DagStepAgentWorkflow],
    )
    print(
        f"Agent DAG worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
