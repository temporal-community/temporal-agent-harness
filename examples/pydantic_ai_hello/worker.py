"""Worker for the hello-world Pydantic AI agent.

Run from the repo root with:
    uv run --group examples python -m examples.pydantic_ai_hello.worker

Hosts only the PydanticAIHelloAgent workflow. Its one tool (`get_weather`) is an inline workflow
tool with no worker-side body, so there are no harness tool activities to register — the Pydantic AI
`AgentPlugin` registers the agent's activities (model request/stream, event_stream_handler, and the
toolset's call_tool) itself.

Two plugins, mirroring the upstream Pydantic AI Temporal setup:
  * ``PydanticAIPlugin`` on the CLIENT — installs the Pydantic-compatible data converter and the
    workflow-sandbox passthroughs the SDK needs.
  * ``AgentPlugin(temporal_agent)`` on the WORKER — registers the durable agent's activities.

The harness streaming path is wired on the agent itself (in workflow.py): its
``event_stream_handler=harness_event_stream_handler`` runs inside the model activity and translates
raw Pydantic AI events onto the harness turn stream. Drop that handler and the model still runs
durably, just without live streaming.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    PYDANTIC_AI_HELLO_TASK_QUEUE              task queue to poll (default: pydantic-ai-hello)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from .workflow import TASK_QUEUE, PydanticAIHelloAgentWorkflow, _TEMPORAL_AGENT


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("PYDANTIC_AI_HELLO_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    # PydanticAIPlugin supplies the Pydantic-compatible data converter + sandbox passthroughs.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[PydanticAIPlugin()])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[PydanticAIHelloAgentWorkflow],
        # No harness tool activities: get_weather is an inline workflow tool. The durable agent's
        # activities are registered by AgentPlugin.
        activities=[],
        plugins=[AgentPlugin(_TEMPORAL_AGENT)],
    )
    print(
        f"Pydantic AI hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
