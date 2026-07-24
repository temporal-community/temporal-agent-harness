"""Worker for the OpenAI-agent-over-Nexus example.

Run from the repo root with:
    uv run --group examples python -m examples.openai_nexus.worker

The sibling of ``examples/openai_hello``'s worker, with one change: model calls
are routed over Nexus to the standalone model router (``nexus/model_router``)
instead of running as the ``invoke_model_activity`` activity. That routing is
wired here via the plugin's workflow-side seam:

    ModelActivityParameters(workflow_model_provider=nexus_model_provider)

See ``nexus_transport.py`` for how that provider swaps the transport. The router
worker must be running too (``just router`` in ``nexus/model_router``); it owns
and creates the endpoint. There is no streaming seam — the router path is
non-streaming, so the agent uses ``Runner.run``.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_NEXUS_TASK_QUEUE                   agent task queue to poll (default: openai-nexus)

Note: OPENAI_API_KEY is needed by the ROUTER worker (which calls OpenAI), not by
this agent worker.
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)

from .nexus_transport import nexus_model_provider
from .workflow import TASK_QUEUE, OpenAINexusAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("OPENAI_NEXUS_TASK_QUEUE", TASK_QUEUE)

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            # THE seam: resolve the model in workflow context and call it directly.
            # nexus_model_provider gives it a Nexus transport (→ the model router),
            # so no invoke_model_activity is used on this path.
            workflow_model_provider=nexus_model_provider,
        ),
    )

    # The plugin supplies its own (OpenAI-aware, pydantic-compatible) data converter.
    # Use the configured Temporal profile if any, else default to a local dev
    # server (matching the router worker) so `just worker` runs with zero config.
    connect_config = ClientConfig.load_client_connect_config()
    if not connect_config.get("target_host"):
        connect_config["target_host"] = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    if not connect_config.get("namespace"):
        connect_config["namespace"] = os.environ.get("TEMPORAL_NAMESPACE", "default")
    client = await Client.connect(**connect_config, plugins=[plugin])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[OpenAINexusAgentWorkflow],
        # No tool activities: get_weather is an inline workflow tool. No model
        # activities are used either — model calls go over Nexus to the router.
        activities=[],
    )
    print(
        f"OpenAI-over-Nexus agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
