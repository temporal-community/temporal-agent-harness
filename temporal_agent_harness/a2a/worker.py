# ABOUTME: Standalone worker exposing A2A plus harness controls over Nexus.
#
# Optional: the service handlers can instead be added to an agent's own Worker (same
# process, same task queue as the workflow) — use this only if you want Nexus traffic
# scaled independently.
#
# Env vars: TEMPORAL_ADDRESS, AGENT_NAMESPACE, AGENT_WORKFLOW_NAME (required),
# AGENT_WORKFLOW_ID_PREFIX, AGENT_TASK_QUEUE, NEXUS_AGENT_TASK_QUEUE — see defaults below.

from __future__ import annotations

import asyncio
import logging
import os

from nexus_a2a import a2a_nexus_data_converter
from temporalio.client import Client
from temporalio.worker import Worker

from temporal_agent_harness.a2a import a2a_nexus_data_converter
from temporal_agent_harness.a2a.adapter import (
    A2AHandlerConfig,
    A2AServiceHandler,
    make_agent_card,
)

from .control_handler import HarnessControlConfig, HarnessControlServiceHandler

logger = logging.getLogger(__name__)


def _env_or_default(key: str, default: str) -> str:
    return os.environ.get(key) or default


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    address = _env_or_default("TEMPORAL_ADDRESS", "localhost:7233")
    agent_namespace = _env_or_default("AGENT_NAMESPACE", "default")
    agent_workflow_name = os.environ.get("AGENT_WORKFLOW_NAME")
    workflow_id_prefix = _env_or_default("AGENT_WORKFLOW_ID_PREFIX", "agent-")
    agent_task_queue = _env_or_default("AGENT_TASK_QUEUE", "agent")
    nexus_task_queue = _env_or_default("NEXUS_AGENT_TASK_QUEUE", "nexus-agent")

    if not agent_workflow_name:
        raise SystemExit("AGENT_WORKFLOW_NAME is required")

    client = await Client.connect(
        address,
        namespace=agent_namespace,
        data_converter=a2a_nexus_data_converter,
    )

    control_config = HarnessControlConfig(workflow_id_prefix=workflow_id_prefix)
    worker = Worker(
        client,
        task_queue=nexus_task_queue,
        nexus_service_handlers=[
            A2AServiceHandler(
                client,
                A2AHandlerConfig(
                    agent_task_queue=agent_task_queue,
                    workflow_name=agent_workflow_name,
                    workflow_id_prefix=workflow_id_prefix,
                    is_message_queuing_enabled=True,
                    agent_card=make_agent_card(
                        name=_env_or_default("AGENT_NAME", agent_workflow_name),
                        description=_env_or_default(
                            "AGENT_DESCRIPTION", "Temporal Agent Harness agent."
                        ),
                        endpoint=_env_or_default(
                            "NEXUS_AGENT_ENDPOINT", "agent-endpoint"
                        ),
                    ),
                ),
            ),
            HarnessControlServiceHandler(client, control_config),
        ],
    )

    logger.info(
        "nexus-agent-py ready: namespace=%s nexusQueue=%s agentQueue=%s "
        "workflow=%s idPrefix=%s",
        agent_namespace,
        nexus_task_queue,
        agent_task_queue,
        agent_workflow_name,
        workflow_id_prefix,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
