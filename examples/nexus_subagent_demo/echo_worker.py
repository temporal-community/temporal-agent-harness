"""Worker for the Echo subagent (see ``echo_workflow.py``).

Self-registers with the agent registry on startup (so a parent's discovery call sees it) and
deregisters on shutdown.

Run from the repo root with:
    uv run python -m examples.nexus_subagent_demo.echo_worker

Prerequisites — see examples/nexus_subagent_demo/README.md:
  - a standalone-Nexus-capable temporal-server (stock `temporal server start-dev` will NOT
    work — see nexus/tests/conftest.py's module docstring for how to build one)
  - the `echo-agent-nexus-endpoint` and `agent-registry-endpoint` Nexus endpoints created
  - the agent registry worker running (`uv run python -m agent_registry.run_worker`)
  - this agent's own Go Nexus front door running (nexus/agent_adapter/nexus_worker, pointed
    at AGENT_WORKFLOW_NAME=EchoSubagent / AGENT_TASK_QUEUE=echo-agent)

Env vars:
    TEMPORAL_CONFIG_FILE           path to temporal.toml
    TEMPORAL_PROFILE               profile name to load (default: "default")
    ECHO_AGENT_TASK_QUEUE          (default: echo-agent)
    ECHO_AGENT_NEXUS_ENDPOINT      this agent's own AgentService Nexus endpoint name
                                    (default: echo-agent-nexus-endpoint)
    AGENT_REGISTRY_NEXUS_ENDPOINT  the agent registry's Nexus endpoint name
                                    (default: agent-registry-endpoint)
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from subagents.registry import (
    deregister_agent_from_registry,
    register_agent_with_registry,
)

from .echo_workflow import ECHO_AGENT_TASK_QUEUE, EchoSubagentWorkflow

AGENT_KEY = "echo"


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    task_queue = os.environ.get("ECHO_AGENT_TASK_QUEUE", ECHO_AGENT_TASK_QUEUE)
    agent_endpoint = os.environ.get("ECHO_AGENT_NEXUS_ENDPOINT", "echo-agent-nexus-endpoint")
    registry_endpoint = os.environ.get("AGENT_REGISTRY_NEXUS_ENDPOINT", "agent-registry-endpoint")

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, data_converter=pydantic_data_converter)

    await register_agent_with_registry(
        client,
        EchoSubagentWorkflow,
        agent_key=AGENT_KEY,
        endpoint=agent_endpoint,
        registry_endpoint=registry_endpoint,
        description="Echoes text back uppercased. Trivial, model-free demo subagent for "
        "exercising the Nexus-brokered subagent path end to end.",
    )

    worker = Worker(client, task_queue=task_queue, workflows=[EchoSubagentWorkflow])
    print(
        f"Echo subagent worker ready: profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"address={connect_config.get('target_host')} "
        f"namespace={connect_config.get('namespace')} "
        f"taskQueue={task_queue} registered as {AGENT_KEY!r} with {registry_endpoint!r}",
        flush=True,
    )
    try:
        await worker.run()
    finally:
        await deregister_agent_from_registry(
            client, agent_key=AGENT_KEY, registry_endpoint=registry_endpoint
        )


if __name__ == "__main__":
    asyncio.run(main())
