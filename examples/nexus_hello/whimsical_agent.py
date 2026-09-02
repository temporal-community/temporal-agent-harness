"""Worker entry point for the whimsical Nexus Hello agent.

The same harness-native agent can be spawned by Nexus Hello or mounted directly from
the account UI. In either role it uses the account's HTTP MCP toolbox plus the native
Nexus MCP service, but answers in a deliberately whimsical voice.

Run from the repository root with:
    uv run --extra nexus-mcp --group examples \
        python -m examples.nexus_hello.whimsical_agent worker
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

from temporal_agent_harness.a2a.adapter import (
    A2AHandlerConfig,
    A2AServiceHandler,
    make_agent_card,
)
from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.nexus_agent_adapter.handler import (
    HarnessControlConfig,
    HarnessControlServiceHandler,
)

from .whimsical_workflow import WORKFLOW_NAME, WhimsicalAgentWorkflow

TASK_QUEUE = "nexus-hello-whimsical-agent"


async def _run_worker() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    plugin = OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            stream_to_provider=stream_to_provider,
        ),
        observer_factory=harness_observer_factory,
    )
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, plugins=[plugin])
    control_config = HarnessControlConfig()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[WhimsicalAgentWorkflow],
        nexus_service_handlers=[
            A2AServiceHandler(
                client,
                A2AHandlerConfig(
                    agent_task_queue=TASK_QUEUE,
                    workflow_name=WORKFLOW_NAME,
                    workflow_id_prefix="",
                    is_message_queuing_enabled=True,
                    agent_card=make_agent_card(
                        name="Whimsical Agent",
                        description="An OpenAI Agents SDK agent with a playful voice.",
                        endpoint="nexus-hello-whimsical-agent-endpoint",
                    ),
                ),
            ),
            HarnessControlServiceHandler(client, control_config),
        ],
    )
    print(
        "Whimsical agent worker + Nexus front door ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={TASK_QUEUE}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] != "worker":
        sys.exit("usage: python -m examples.nexus_hello.whimsical_agent worker")
    asyncio.run(_run_worker())
