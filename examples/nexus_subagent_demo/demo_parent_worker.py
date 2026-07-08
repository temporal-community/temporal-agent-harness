"""Worker for the subagent demo parent (see ``demo_parent_workflow.py``).

Run from the repo root with:
    uv run python -m examples.nexus_subagent_demo.demo_parent_worker

See examples/nexus_subagent_demo/README.md for the full command sequence, including how to
start this workflow and drive a turn with the plain `temporal workflow` CLI (no bespoke
driver script needed). The model-driven `ask` handler needs GEMINI_API_KEY; the model-free
`echo_via_subagent`/`list_dynamic_tools`/`dynamic_call` handlers don't.

Env vars:
    TEMPORAL_CONFIG_FILE      path to temporal.toml
    TEMPORAL_PROFILE          profile name to load (default: "default")
    SUBAGENT_DEMO_TASK_QUEUE  (default: subagent-demo-parent)
    GEMINI_API_KEY            required for the `ask` handler
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from google.genai import Client as GeminiClient
from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from .demo_parent_workflow import DEMO_PARENT_TASK_QUEUE, SubagentDemoParentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    task_queue = os.environ.get("SUBAGENT_DEMO_TASK_QUEUE", DEMO_PARENT_TASK_QUEUE)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    plugin = GoogleGenAIPlugin(GeminiClient(api_key=api_key))

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[plugin],
        data_converter=pydantic_data_converter,
    )

    worker = Worker(client, task_queue=task_queue, workflows=[SubagentDemoParentWorkflow])
    print(
        f"Subagent demo parent worker ready: profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
