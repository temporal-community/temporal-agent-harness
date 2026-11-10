"""Worker for the hello-world Strands Agents agent.

Hosts the StrandsHelloAgent workflow. Two plugins on the client:
  * ``StrandsPlugin`` — the Strands model activity (with the harness observer that streams each
    model call onto its turn), the ``scratch`` sandbox's activities, the data converter, and the
    workflow-sandbox passthroughs Strands needs.
  * ``AgentHarnessPlugin``, after it — the harness's own activities, plus the activity body of
    ``get_local_time``, the agent's activity tool.

The ``scratch`` sandbox is Strands' ``NotASandboxLocalEnvironment``: commands and file edits run
on this worker's machine, in its working directory, with no isolation. That keeps the example free
of Docker; the agent's approval policy makes every sandbox call wait for a person's approval. Give
a real deployment a ``DockerSandbox`` or ``SshSandbox`` instead.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    STRANDS_HELLO_TASK_QUEUE                  task queue to poll (default: strands-hello)
    TASK_QUEUE_PREFIX                         put in front of the default queue's name, if set
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from strands.models.openai import OpenAIModel
from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from examples.strands_hello.workflow import (
    MODEL,
    SANDBOX,
    TASK_QUEUE,
    StrandsHelloAgentWorkflow,
    get_local_time,
)
from temporal_agent_harness.ai_sdks.strands_agents import StrandsPlugin
from temporal_agent_harness.ai_sdks.strands_agents_harness import harness_observer_factory
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.utils.task_queue import task_queue_name

OPENAI_MODEL_ID = "gpt-5.1"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    task_queue = os.environ.get("STRANDS_HELLO_TASK_QUEUE") or task_queue_name(TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    strands = StrandsPlugin(
        # Built lazily in the model activity, which reads OPENAI_API_KEY from the environment.
        models={MODEL: lambda: OpenAIModel(model_id=OPENAI_MODEL_ID)},
        sandboxes={SANDBOX: lambda _context: NotASandboxLocalEnvironment()},
        observer_factory=harness_observer_factory,
    )
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config, plugins=[strands, AgentHarnessPlugin(tools=[get_local_time])]
    )

    worker = Worker(client, task_queue=task_queue, workflows=[StrandsHelloAgentWorkflow])
    print(
        f"Strands hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
