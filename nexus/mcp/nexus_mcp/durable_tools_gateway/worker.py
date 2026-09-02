"""Durable Tool Call Gateway Temporal worker: registers 3rd-party MCP servers and
proxies their tool calls as standalone activities (Nexus + SAA).

The same worker now registers and proxies HTTP A2A subagents.

Requires server-side dynamic config: `activity.enableStandalone`,
`nexusoperation.enableStandalone`. See examples/nexus_hello/justfile's `temporal` recipe.

Usage (from repo root):
    uv run --extra nexus-mcp --group examples python -m nexus_mcp.durable_tools_gateway.worker

Env vars:
    GATEWAY_CATALOG_FILE            JSON file of shared ResourceDescriptor objects.
    GATEWAY_UI_ACCOUNT_ID           Serve the account UI from this worker when set.
    GATEWAY_UI_PORT                 Account UI port (default: 8000).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from nexus_a2a import NexusA2AServiceHandler, a2a_nexus_data_converter
import uvicorn
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .agent_broker import AgentDiscoveryWorkflow
from .registry import (
    GLOBAL_CATALOG_WORKFLOW_ID,
    REGISTRY_TASK_QUEUE,
    GlobalCatalogWorkflow,
    ToolRegistryWorkflow,
    account_registry_workflow_id,
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
from .resources import ResourceDescriptor, descriptor_from_dict

logger = logging.getLogger(__name__)


async def _ensure_account(client: Client, account_id: str):
    return await client.start_workflow(
        ToolRegistryWorkflow.run,
        account_id,
        id=account_registry_workflow_id(account_id),
        task_queue=REGISTRY_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def _ensure_catalog(client: Client):
    return await client.start_workflow(
        GlobalCatalogWorkflow.run,
        id=GLOBAL_CATALOG_WORKFLOW_ID,
        task_queue=REGISTRY_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def main(
    ui_account_id: str | None = None,
    ui_port: int = 8000,
    catalog_resources: list[ResourceDescriptor] | None = None,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(a2a_nexus_data_converter),
    )

    worker = Worker(
        client,
        task_queue=REGISTRY_TASK_QUEUE,
        workflows=[
            GlobalCatalogWorkflow,
            ToolRegistryWorkflow,
            AgentDiscoveryWorkflow,
        ],
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
        logger.info(
            "Durable Tool Call Gateway ready — task_queue=%r", REGISTRY_TASK_QUEUE
        )
        catalog = await _ensure_catalog(client)
        if catalog_resources:
            await catalog.execute_update(
                GlobalCatalogWorkflow.publish_resources,
                catalog_resources,
                id="publish-catalog-"
                + "-".join(
                    f"{item.resource_id}-r{item.revision}" for item in catalog_resources
                ),
                result_type=list[ResourceDescriptor],
            )
            logger.info("Published %d global catalog resources", len(catalog_resources))
        if ui_account_id:
            from .web import create_account_agent_app

            await _ensure_account(client, ui_account_id)
            server = uvicorn.Server(
                uvicorn.Config(
                    create_account_agent_app(ui_account_id, temporal_client=client),
                    host="0.0.0.0",
                    port=ui_port,
                    log_level="info",
                )
            )
            logger.info(
                "Account UI ready — account_id=%r port=%d", ui_account_id, ui_port
            )
            await server.serve()
        else:
            await asyncio.Event().wait()


if __name__ == "__main__":
    catalog_file = os.environ.get("GATEWAY_CATALOG_FILE")
    catalog_resources = (
        [
            descriptor_from_dict(item)
            for item in json.loads(Path(catalog_file).read_text())
        ]
        if catalog_file
        else None
    )
    asyncio.run(
        main(
            os.environ.get("GATEWAY_UI_ACCOUNT_ID"),
            int(os.environ.get("GATEWAY_UI_PORT", "8000")),
            catalog_resources,
        )
    )
