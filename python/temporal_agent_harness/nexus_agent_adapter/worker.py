# ABOUTME: Standalone Nexus worker exposing AgentService on its own task queue.
#
# AgentServiceHandler can instead be added to the agent's own worker (same process and task
# queue as the workflow); run this only to scale Nexus traffic independently.
#
# Connects like the packaged `serve` command: a temporal.toml profile plus TEMPORAL_* env vars
# (TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, ...), with the harness's data converter.
# Env vars: AGENT_WORKFLOW_NAME (required), AGENT_WORKFLOW_ID_PREFIX, AGENT_TASK_QUEUE,
# NEXUS_AGENT_TASK_QUEUE — see the defaults below.

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.worker import Worker

from temporal_agent_harness.nexus_agent_adapter.handler import AgentServiceHandler, Config
from temporal_agent_harness.web.client import connect_client, ensure_temporal_address

logger = logging.getLogger(__name__)


def _env_or_default(key: str, default: str) -> str:
    return os.environ.get(key) or default


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    agent_workflow_name = os.environ.get("AGENT_WORKFLOW_NAME")
    workflow_id_prefix = _env_or_default("AGENT_WORKFLOW_ID_PREFIX", "agent-")
    agent_task_queue = _env_or_default("AGENT_TASK_QUEUE", "agent")
    nexus_task_queue = _env_or_default("NEXUS_AGENT_TASK_QUEUE", "nexus-agent")

    if not agent_workflow_name:
        raise SystemExit("AGENT_WORKFLOW_NAME is required")

    ensure_temporal_address(None)
    client = await connect_client()

    config = Config(
        agent_task_queue=agent_task_queue,
        workflow_name=agent_workflow_name,
        workflow_id_prefix=workflow_id_prefix,
    )
    worker = Worker(
        client,
        task_queue=nexus_task_queue,
        nexus_service_handlers=[AgentServiceHandler(client, config)],
    )

    logger.info(
        "AgentService ready: namespace=%s nexusQueue=%s agentQueue=%s workflow=%s idPrefix=%s",
        client.namespace,
        nexus_task_queue,
        agent_task_queue,
        agent_workflow_name,
        workflow_id_prefix,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
