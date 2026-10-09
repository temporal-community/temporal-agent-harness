"""Worker for the auto-mode travel agent.

Run from the repo root with:
    uv run python -m examples.auto_mode.worker

(or `just worker` from examples/auto_mode, which installs the project's `examples` dependency
group first.)

Connection settings come from a ``temporal.toml`` profile, resolved through temporalio's
``ClientConfig.load_client_connect_config()``, which reads TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE
from the environment. The example sets these in ``.env.local`` (see examples/auto_mode/README.md).

Env vars:
    TEMPORAL_CONFIG_FILE         path to a temporal.toml (set in .env.local)
    TEMPORAL_PROFILE             profile name to load (default: "default")
    GEMINI_API_KEY               required — the agent converses via the Gemini Interactions API
    TYPESAFE_API_KEY             required — auto mode judges gated calls with Jev
                                 (TYPESAFE_AI_API_KEY is accepted as an alias)
    AUTO_MODE_AGENT_TASK_QUEUE   task queue to poll (default: auto-mode-agent)

Both keys are required up front, and that is deliberate even though a missing TypeSafe key
would not wedge anything: the Jev activity would fail, and every gated call would fall through
to the human gate. The agent would run, but auto mode would never approve anything, which
looks like "auto mode doesn't work" rather than "the key is missing". Failing at startup names
the actual problem. The Monty example (examples/monty) is the same agent without auto mode,
and needs only the Gemini key.

This worker hosts only the auto-mode agent, not the session manager. The packaged session
manager is hosted by `temporal-agent-harness session-manager`; because it launches agents by
registered name, it dispatches this agent to this queue without this worker hosting it.
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
from temporalio.typesafe import TypeSafePlugin
from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

from temporal_agent_harness.ai_sdks.google_genai_plugin import GoogleGenAIPlugin
from temporal_agent_harness.harness.jev_approvals.activity import typesafe_api_key
from temporal_agent_harness.plugin import AgentHarnessPlugin

from ..monty import activities
from .workflow import TASK_QUEUE, AutoModeTravelAgentWorkflow


async def main() -> None:
    # force=True so our config wins even if an imported module already installed a root
    # handler. The temporalio loggers are pinned to INFO so the per-step Code Mode trace and
    # each auto-mode evaluation are visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    for name in ("temporalio", "temporalio.workflow", "temporalio.activity"):
        logging.getLogger(name).setLevel(logging.INFO)

    task_queue = os.environ.get("AUTO_MODE_AGENT_TASK_QUEUE", TASK_QUEUE)

    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        sys.exit("error: GEMINI_API_KEY env var not set")
    if not typesafe_api_key():
        sys.exit(
            "error: TYPESAFE_API_KEY env var not set — auto mode judges gated calls with "
            "Jev. For the same agent without auto mode, run examples/monty instead."
        )

    # The harness comes last so provider activities and payload converters are preserved.
    # TypeSafePlugin owns the Jev request; the evaluator owns approval thresholds.
    connect_config = ClientConfig.load_client_connect_config()
    async with AsyncTypeSafeClient(
        api_key=typesafe_api_key(), retry=RetryPolicy(max_retries=0),
    ) as provider:
        client = await Client.connect(
            **connect_config,
            plugins=[
                GoogleGenAIPlugin(GeminiClient(api_key=gemini_api_key)),
                TypeSafePlugin(provider),
                AgentHarnessPlugin(tools=activities.ALL_TOOLS),
            ],
        )

        worker = Worker(client, task_queue=task_queue, workflows=[AutoModeTravelAgentWorkflow])
        print(
            f"Auto-mode agent worker ready: "
            f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
            f"address={connect_config.get('target_host')} "
            f"namespace={connect_config.get('namespace')} "
            f"taskQueue={task_queue}",
            flush=True,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
