"""Worker for the hello-world OpenAI Agents agent.

Run from the repo root with:
    uv run --group examples python -m examples.openai_hello.worker

Hosts only the OpenAIHelloAgent workflow. Its one tool (`get_weather`) is an inline workflow
tool with no worker-side body, so there are no tool activities to register — the OpenAI Agents
plugin registers the model activities (including the streaming one) itself.

The plugin is wired for the HARNESS STREAMING PATH:
  * ``model_params.stream_to_provider=stream_to_provider`` — resolves each streamed model
    call's per-turn stream context ambiently off the running workflow, and
  * ``observer_factory=harness_observer_factory`` — turns that context into the observer that
    translates raw OpenAI events into the harness turn-stream vocabulary live.
Drop either one and streaming falls back to the plugin's plain raw-topic behavior.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    OPENAI_HELLO_TASK_QUEUE                   task queue to poll (default: openai-hello)
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

from .workflow import TASK_QUEUE, OpenAIHelloAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("OPENAI_HELLO_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            # Streaming leans on activity heartbeats to notice a stuck model call; keep the
            # heartbeat timeout well under the overall start-to-close.
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            # The harness streaming seam: route streamed events to the in-flight turn.
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )

    # The plugin supplies its own (OpenAI-aware, pydantic-compatible) data converter.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[OpenAIHelloAgentWorkflow],
        # No tool activities: get_weather is an inline workflow tool. The OpenAI model
        # activities (incl. invoke_model_activity_streaming) are registered by the plugin.
        activities=[],
    )
    print(
        f"OpenAI hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
