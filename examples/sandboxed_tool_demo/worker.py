"""Worker for the interactive sandboxed-tool demo agent.

Run from the repo root with:
    uv run --extra sandbox --group examples python -m examples.sandboxed_tool_demo.worker

Hosts SandboxedToolDemoAgent plus its sandbox lifecycle activities (SANDBOX_ACTIVITIES) and its
one sandboxed tool's activity (run_bash). The Gemini plugin is registered because this agent
drives the Gemini Interactions API to converse and decide when to call run_bash; the plugin
auto-registers its interactions activity. Run `just build-sandbox` (or
`temporal_agent_harness.harness.sandbox.build_sandbox`) once before starting this worker —
runtime never builds the sandbox image implicitly (SandboxConfig.require_prebuilt).

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    GEMINI_API_KEY                            required — the agent calls the Gemini API
    DAYTONA_API_KEY                           required — tools.py's SANDBOX runs on Daytona
    SANDBOXED_TOOL_DEMO_TASK_QUEUE            task queue to poll (default: sandboxed-tool-demo)
"""

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
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.sandbox.activities import SANDBOX_ACTIVITIES

from .tools import run_bash
from .workflow import TASK_QUEUE, SandboxedToolDemoAgent


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("SANDBOXED_TOOL_DEMO_TASK_QUEUE", TASK_QUEUE)

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

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[SandboxedToolDemoAgent],
        # SANDBOX_ACTIVITIES: sandbox activate/pause/terminate. tool_activity(run_bash): the one
        # sandboxed tool's durable body. The Gemini interactions activity is registered by the
        # plugin above.
        activities=[*SANDBOX_ACTIVITIES, agent.tool_activity(run_bash)],
    )
    print(
        f"sandboxed-tool-demo worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
