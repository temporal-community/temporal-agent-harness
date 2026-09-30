"""Worker for the Sano Hello agent.

Run from the repo root with:
    uv run --group examples python -m examples.sano_hello.worker

One worker on one task queue hosts:
  * the SanoHelloAgent workflow and its tool activities, and
  * the AgentService Nexus handler. The Nexus endpoint in agents.toml targets this task
    queue, so the web UI reaches the agent through standalone Nexus operations.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
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
from temporal_agent_harness.nexus_agent_adapter.handler import (
    AgentServiceHandler,
    Config,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .tools import ALL_TOOLS
from .workflow import TASK_QUEUE, WORKFLOW_NAME, SanoHelloAgentWorkflow

# AgentService maps a session ID to the workflow ID with this prefix.
WORKFLOW_ID_PREFIX = "sano-hello-"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            # Route streamed model events to the in-flight turn.
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )
    # Harness plugin last, so the OpenAI plugin's payload converter wins.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config, plugins=[plugin, AgentHarnessPlugin(tools=ALL_TOOLS)]
    )

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SanoHelloAgentWorkflow],
        nexus_service_handlers=[
            AgentServiceHandler(
                client,
                Config(
                    agent_task_queue=TASK_QUEUE,
                    workflow_name=WORKFLOW_NAME,
                    workflow_id_prefix=WORKFLOW_ID_PREFIX,
                ),
            )
        ],
    )
    print(
        f"Sano hello agent worker ready: "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={TASK_QUEUE}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
