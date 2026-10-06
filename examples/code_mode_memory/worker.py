"""Worker for the Code Mode memory example agent.

Run from the repo root with:
    uv run --group examples python -m examples.code_mode_memory.worker

(or `just worker` from examples/code_mode_memory).

Connection settings come from a ``temporal.toml`` profile, resolved through temporalio's
``ClientConfig.load_client_connect_config()`` (TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE, set in
``.env.local``).

Env vars:
    TEMPORAL_CONFIG_FILE    path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE        profile name to load (default: "default")
    GEMINI_API_KEY          required — the agent calls the Gemini Interactions API
"""

from __future__ import annotations

import asyncio
import os
import sys

from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .activities import load_skills
from .local_disk import LocalDisk
from .workflow import TASK_QUEUE, CodeModeMemoryAgentWorkflow


async def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")

    # Harness plugin LAST so the Gemini plugin's payload converter wins. load_skills seeds
    # /skills; LocalDisk's activities are /memory, so this worker's disk holds the memory.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[
            GoogleGenAIPlugin(GeminiClient(api_key=api_key)),
            AgentHarnessPlugin(filesystems=[LocalDisk]),
        ],
    )
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CodeModeMemoryAgentWorkflow],
        activities=[load_skills],
    )
    print(f"Code Mode memory worker ready: taskQueue={TASK_QUEUE}", flush=True)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
