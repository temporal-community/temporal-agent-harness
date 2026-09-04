"""Worker for the Nexus-hello agent.

Run from the repo root with:
    uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.worker

No Nexus-related plugin config here. workflow.py wires its own tools directly via
nexus_native_mcp_server(...) and nexus_tools_gateway().
The worker also hosts the agent's A2A and harness-control Nexus services.

Env vars (set in .env.local - see .env.example):
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE   Temporal connection profile (this worker's own
                                               namespace, e.g. "default")
    OPENAI_API_KEY                            required - the agent calls the OpenAI API
    NEXUS_HELLO_TASK_QUEUE                    task queue to poll (default: nexus-hello)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import timedelta

from nexus_a2a import NexusA2AServiceHandler, a2a_nexus_data_converter, make_agent_card
from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.a2a.adapter import (
    HarnessA2ABackend,
    HarnessA2ABackendConfig,
)
from temporal_agent_harness.a2a.control_handler import (
    HarnessControlConfig,
    HarnessControlServiceHandler,
)
from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)

from .workflow import TASK_QUEUE, WORKFLOW_NAME, NexusHelloAgentWorkflow


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
    )

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        plugins=[plugin],
        data_converter=a2a_nexus_data_converter,
    )
    control_config = HarnessControlConfig()

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[NexusHelloAgentWorkflow],
        nexus_service_handlers=[
            NexusA2AServiceHandler(
                HarnessA2ABackend(
                    client,
                    HarnessA2ABackendConfig(
                        agent_task_queue=task_queue,
                        workflow_name=WORKFLOW_NAME,
                        workflow_id_prefix="",
                        is_message_queuing_enabled=True,
                        agent_card=make_agent_card(
                            name="Nexus Hello",
                            description="OpenAI Agents SDK demo with Nexus tools and subagents.",
                            endpoint="nexus-hello-agent-endpoint",
                        ),
                    ),
                )
            ),
            HarnessControlServiceHandler(client, control_config),
        ],
    )
    print(
        f"Nexus hello agent worker + A2A Nexus front door ready: "
        f"profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
