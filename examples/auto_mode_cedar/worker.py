"""Worker for the Cedar auto-mode travel agent.

Run from the repo root with:
    uv run python -m examples.auto_mode_cedar.worker

(or `just worker` from examples/auto_mode_cedar.)

Env vars:
    TEMPORAL_CONFIG_FILE               path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE                   profile name to load (default: "default")
    GEMINI_API_KEY                     required — the agent converses via the Gemini Interactions API
    TYPESAFE_API_KEY                   required — Jev classifies each script for the policy
                                       (TYPESAFE_AI_API_KEY is accepted as an alias)
    AUTO_MODE_CEDAR_AGENT_TASK_QUEUE   task queue to poll (default: auto-mode-cedar-agent)

The policy itself is not this worker's: the Cedar verifier serves it behind the `tool-verifier`
Nexus endpoint (see README.md). The agent calls it from the workflow, so this worker registers
nothing for it. A missing verifier shows up as every gated call escalating to you, with the
reason on its `auto_approval_evaluation_error` event.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from google.genai import Client as GeminiClient
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.harness.jev_approvals.activity import typesafe_api_key
from temporal_agent_harness.plugin import AgentHarnessPlugin

from ..monty import activities
from .workflow import TASK_QUEUE, AutoModeCedarTravelAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    task_queue = os.environ.get("AUTO_MODE_CEDAR_AGENT_TASK_QUEUE", TASK_QUEUE)

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    if not typesafe_api_key():
        sys.exit(
            "error: TYPESAFE_API_KEY env var not set — Jev classifies each script before the "
            "Cedar policy judges it."
        )

    # Harness plugin LAST so the Gemini plugin's payload converter wins. The harness plugin
    # registers the travel tools' activities and the `jev_classify` activity the script
    # classifier dispatches by name.
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[
            GoogleGenAIPlugin(GeminiClient(api_key=gemini_api_key)),
            AgentHarnessPlugin(tools=activities.ALL_TOOLS),
        ],
    )

    worker = Worker(client, task_queue=task_queue, workflows=[AutoModeCedarTravelAgentWorkflow])
    print(
        f"Cedar auto-mode agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
