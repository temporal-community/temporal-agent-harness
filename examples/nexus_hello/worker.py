"""Worker for the Nexus-transport hello-world agent.

Run from the repo root with:
    uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.worker

OpenAIAgentsPlugin(nexus_transport=True) below is the ONLY place this example mentions Nexus
at all on the agent side — workflow.py's Agent(...) call has no mcp_servers=[...], no
registry, nothing. Every Agent this worker runs automatically gets a Nexus-transport MCP
server appended to it, which reaches every tool source over Nexus, uniformly — a registered
Nexus-native server directly, or the Durable Tools Gateway's RegistryService.call_tool
(which starts ToolCallWorkflow, a plain workflow wrapping one activity, ON THE GATEWAY'S OWN worker/task-queue) for
everything else. Which of those a given conversation can actually reach is entirely
self-serve, registered live against that workflow's own registry (see workflow.py's
NexusHelloAgentWorkflow.__init__) — nothing about it is worker-level config. So this worker
needs no registry client and no activities of its own at all — every hop that leaves this
namespace goes through a Nexus endpoint.

Env vars (set in .env.local — see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile (this worker's own
                                               namespace, e.g. "default")
    OPENAI_API_KEY                            required — the agent calls the OpenAI API
    NEXUS_HELLO_TASK_QUEUE                    task queue to poll (default: nexus-hello)
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
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)

from .workflow import TASK_QUEUE, NexusHelloAgentWorkflow


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    task_queue = os.environ.get("NEXUS_HELLO_TASK_QUEUE", TASK_QUEUE)

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
        # The one line that wires up Nexus for every Agent this worker runs -- see
        # workflow.py's Agent(...) call, which mentions Nexus nowhere at all. What each
        # conversation can actually reach through it (gateway, 1st-party Nexus servers, or
        # both) is registered live, per-workflow -- see NexusHelloAgentWorkflow.__init__.
        nexus_transport=True,
    )

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[NexusHelloAgentWorkflow],
    )
    print(
        f"Nexus hello agent worker ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
