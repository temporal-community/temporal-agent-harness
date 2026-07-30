"""Worker for the Nexus-transport hello-world agent.

Run from the repo root with:
    uv run --extra nexus-mcp --group examples python -m examples.nexus_hello.worker

OpenAIAgentsPlugin(nexus_mcp_initial_servers={...}) below is the only place this example
mentions Nexus at all -- both demo tools' names/endpoints live here, not split across this
file and workflow.py. This automatically enables using Nexus to broker MCP client <-> server
and pre-registers both demo tools; register more, live, via a register_mcp_server signal
against the harness (i.e., via Temporal CLI, SDK, etc...)

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

# Must match durable_tools_gateway.REGISTRY_SERVICE_NAME / REGISTRY_NEXUS_ENDPOINT and the
# justfile's gateway_service_name / registry_endpoint.
GATEWAY_SERVICE_NAME = "RegistryService"
GATEWAY_ENDPOINT = "mcp-registry-endpoint"
# Must match nexus_tool_service.py's SERVICE_NAME / NEXUS_ENDPOINT.
DEMO_NEXUS_SERVICE_NAME = "demo-nexus"
DEMO_NEXUS_ENDPOINT = "nexus-hello-demo-endpoint"


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
        # Enables Nexus-transport MCP, pre-registered with both demo tools -- no
        # register_mcp_server signal needed to use them.
        # If we wanted to register more Nexus MCP servers, we can still register them
        # dynamically mid-turns later (via a signal handler).
        nexus_mcp_initial_servers={
            GATEWAY_SERVICE_NAME: GATEWAY_ENDPOINT,
            DEMO_NEXUS_SERVICE_NAME: DEMO_NEXUS_ENDPOINT,
        },
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
