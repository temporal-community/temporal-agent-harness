"""Worker for the hello-world OpenAI Codex agent.

Run from the repo root with:
    uv run --group examples python -m examples.codex_hello.worker

Hosts only the CodexHelloAgent workflow. Its one tool (`get_weather`) is an inline workflow tool with
no worker-side body, so there are no tool activities to register — `CodexHarnessPlugin` registers the
Codex segment activity and `AgentHarnessPlugin` supplies the harness's data converter and activities.

Two modes:
  * real (default): Codex talks to OpenAI; needs OPENAI_API_KEY in the environment.
  * fake (CODEX_HELLO_FAKE=1): Codex talks to a local scripted Responses server, so you can see the
    whole stack without credentials. The fake "model" follows ``CALL:<tool>|<json>`` lines you type
    in the chat, e.g.  `weather?\nCALL:get_weather|{"city":"Paris"}`.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required unless CODEX_HELLO_FAKE=1
    CODEX_HELLO_TASK_QUEUE                    task queue to poll (default: codex-hello)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.codex_harness import CodexHarnessPlugin
from temporalio.openai_codex.testing import FakeResponsesServer
from temporal_agent_harness.plugin import AgentHarnessPlugin

from .workflow import TASK_QUEUE, CodexHelloAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("CODEX_HELLO_TASK_QUEUE", TASK_QUEUE)

    fake: FakeResponsesServer | None = None
    config_overrides: list[str] = []
    if os.environ.get("CODEX_HELLO_FAKE") == "1":
        fake = FakeResponsesServer()
        await fake.start()
        config_overrides = fake.config_overrides
        print(f"Codex model: fake Responses server on :{fake.port} (CODEX_HELLO_FAKE=1)", flush=True)
    elif not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set (or set CODEX_HELLO_FAKE=1 for the fake model)")

    # Two plugins, harness LAST (see AgentHarnessPlugin's notes on ordering).
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[CodexHarnessPlugin(config_overrides=config_overrides), AgentHarnessPlugin()],
    )

    worker = Worker(client, task_queue=task_queue, workflows=[CodexHelloAgentWorkflow])
    print(
        f"Codex hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    try:
        await worker.run()
    finally:
        if fake is not None:
            await fake.stop()


if __name__ == "__main__":
    asyncio.run(main())
