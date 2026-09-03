"""Durable Tool Call Gateway Temporal worker: registers 3rd-party MCP servers and
proxies their tool calls as standalone activities (Nexus + SAA).

The same worker now registers and proxies HTTP A2A subagents.

Requires server-side dynamic config: `activity.enableStandalone`,
`nexusoperation.enableStandalone`. See examples/nexus_hello/justfile's `temporal` recipe.

Usage (from repo root):
    uv run --extra nexus-mcp --group examples python -m nexus_mcp.durable_tools_gateway.worker

Env vars:
    GATEWAY_SEED_EXTERNAL_SERVERS   JSON {"name": "url", ...} to register on startup.
    GATEWAY_SEED_AGENT_ID           agent_id to register seeded servers under. Required
                                     if GATEWAY_SEED_EXTERNAL_SERVERS is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from nexus_a2a import NexusA2AServiceHandler, a2a_nexus_data_converter
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .registry import (
    REGISTRY_TASK_QUEUE,
    REGISTRY_WORKFLOW_ID,
    ToolRegistryWorkflow,
    fetch_external_tools,
)
from .registry_service_handler import (
    GatewayA2ABackend,
    RegistryServiceHandler,
    mcp_proxy_activity,
    subagent_proxy_activity,
    subagent_start_activity,
    subagent_stop_activity,
)

logger = logging.getLogger(__name__)


async def _seed_external_servers(client: Client, seed: dict[str, str], agent_id: str) -> None:
    """Signal each {name: url} pair to ToolRegistryWorkflow.register_external, under
    one agent_id."""
    registry_handle = client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
    for name, url in seed.items():
        await registry_handle.signal(
            ToolRegistryWorkflow.register_external, args=[agent_id, name, url]
        )
        logger.info("Seeded external server %r -> %s (agent_id=%r)", name, url, agent_id)


async def main(
    seed_external_servers: dict[str, str] | None = None, seed_agent_id: str | None = None
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if seed_external_servers and not seed_agent_id:
        raise SystemExit("GATEWAY_SEED_AGENT_ID is required when GATEWAY_SEED_EXTERNAL_SERVERS is set")

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(a2a_nexus_data_converter),
    )

    await client.start_workflow(
        ToolRegistryWorkflow.run,
        id=REGISTRY_WORKFLOW_ID,
        task_queue=REGISTRY_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )

    worker = Worker(
        client,
        task_queue=REGISTRY_TASK_QUEUE,
        workflows=[ToolRegistryWorkflow],
        activities=[
            mcp_proxy_activity,
            fetch_external_tools,
            subagent_start_activity,
            subagent_proxy_activity,
            subagent_stop_activity,
        ],
        nexus_service_handlers=[
            RegistryServiceHandler(client),
            NexusA2AServiceHandler(GatewayA2ABackend(client)),
        ],
    )
    async with worker:
        logger.info("Durable Tool Call Gateway ready — task_queue=%r", REGISTRY_TASK_QUEUE)
        if seed_external_servers:
            assert seed_agent_id is not None
            await _seed_external_servers(client, seed_external_servers, seed_agent_id)
        await asyncio.Event().wait()


if __name__ == "__main__":
    seed_json = os.environ.get("GATEWAY_SEED_EXTERNAL_SERVERS")
    asyncio.run(
        main(
            json.loads(seed_json) if seed_json else None,
            os.environ.get("GATEWAY_SEED_AGENT_ID"),
        )
    )
