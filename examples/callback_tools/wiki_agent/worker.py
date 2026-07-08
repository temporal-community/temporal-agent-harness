"""Worker for the wiki callback-tools agent.

Run from the repo root with:
    uv run --group examples python -m examples.callback_tools.wiki_agent.worker

(or `just worker` from examples/callback_tools/wiki_agent).

Hosts only the WikiAgent workflow. Its tools are CALLBACK tools — inline workflow tools with no
worker-side body — so there are no tool activities to register; the user's terminal client
(``client.py``) supplies each tool's result. The Gemini plugin is registered because the agent
drives the Gemini Interactions API to converse and decide what to do; the plugin auto-registers
its interactions activity.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    GEMINI_API_KEY                            required — the agent calls the Gemini API
    WIKI_AGENT_TASK_QUEUE                     task queue to poll (default: wiki-agent)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .workflow import TASK_QUEUE, WikiAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    task_queue = os.environ.get("WIKI_AGENT_TASK_QUEUE", TASK_QUEUE)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    plugin = GoogleGenAIPlugin(GeminiClient(api_key=api_key))

    # Match the session-manager worker + server converter (large-payload offload) so every
    # process reads the same payloads.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[plugin],
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[WikiAgentWorkflow],
        # No tool activities: the wiki tools are callback tools fulfilled by the client. The
        # Gemini interactions activity is registered by the plugin above.
        activities=[],
    )
    print(
        f"Wiki agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
