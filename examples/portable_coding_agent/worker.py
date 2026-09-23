"""Workers for the portable coding agent.

Run from the repo root with:
    uv run --group examples python -m examples.portable_coding_agent.worker

Two workers share one process here for convenience. In a fleet, split them:
run SESSION workers (which host the sandboxes) and MODEL workers.

- The SESSION worker hosts the workflow and the sandbox backend on ``TASK_QUEUE``.
  The ``SandboxClientProvider`` is where the real sandbox (a Docker container, or
  the unix-local backend) actually runs; the workflow reaches it through the
  activities the plugin registers here. Eager execution stays on and the sticky
  timeout is short so a session's sandbox operations return to the worker that
  holds its container.
- The MODEL worker hosts the OpenAI model activities on ``MODEL_QUEUE``.

Env vars (set in .env.local; see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required; the agent calls the OpenAI API
    CODING_AGENT_SANDBOX                       docker (default) | local
    CODING_AGENT_SANDBOX_IMAGE                 docker image (default: python:3.12-slim)
    CODING_AGENT_TASK_QUEUE                    session task queue (default: portable-coding-agent)
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
    SandboxClientProvider,
)
from temporal_agent_harness.harness import agent

from .codebase_search import codebase_search
from .sandbox import SANDBOX_NAME, build_sandbox_client, sandbox_kind
from .web import web_fetch
from .workflow import MODEL_QUEUE, TASK_QUEUE, PortableCodingAgentWorkflow


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    session_queue = os.environ.get("CODING_AGENT_TASK_QUEUE", TASK_QUEUE)
    model_queue = f"{session_queue}-model" if session_queue != TASK_QUEUE else MODEL_QUEUE

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    # Register the sandbox backend + route model calls to their own queue.
    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            task_queue=model_queue,
            start_to_close_timeout=timedelta(minutes=3),
        ),
        sandbox_clients=[SandboxClientProvider(SANDBOX_NAME, build_sandbox_client())],
    )
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])

    session_worker = Worker(
        client,
        task_queue=session_queue,
        workflows=[PortableCodingAgentWorkflow],
        # codebase_search and web_fetch are the activity-backed tools; the rest are the sandbox's
        # tools, the inline plan/subagent tools, and the ask_user callback. The plugin registers the
        # sandbox provider + model activities.
        activities=[agent.tool_activity(codebase_search), agent.tool_activity(web_fetch)],
        # Keep a session's sandbox operations returning to the worker that holds its container:
        # eager execution on, and a short sticky timeout so a lost worker is re-dispatched fast.
        disable_eager_activity_execution=False,
        sticky_queue_schedule_to_start_timeout=timedelta(seconds=5),
    )
    model_worker = Worker(client, task_queue=model_queue, activities=[])
    print(
        f"portable coding agent workers ready: session={session_queue!r} model={model_queue!r} "
        f"sandbox={sandbox_kind()!r}",
        flush=True,
    )
    await asyncio.gather(session_worker.run(), model_worker.run())


if __name__ == "__main__":
    asyncio.run(main())
