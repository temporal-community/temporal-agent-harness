"""Run the Durable Tools Gateway worker.

The gateway proxies HTTP MCP servers and subagents with standalone activities.

Requires server-side dynamic config: `activity.enableStandalone`,
`nexusoperation.enableStandalone`. See examples/nexus_hello/justfile's `temporal` recipe.

Usage (from repo root):
    uv run --extra nexus-mcp --group examples python -m durable_tools_gateway.worker

Env vars:
    GATEWAY_SEED_EXTERNAL_SERVERS   JSON {"name": "url", ...} to register on startup.
    GATEWAY_CATALOG_FILE            JSON file of shared ResourceDescriptor objects.
    GATEWAY_SEED_ACCOUNT_ID         account_id to register seeded servers under. Required
                                     if either seed setting is used.
    GATEWAY_UI_ACCOUNT_ID           Serve the account UI from this worker when set.
    GATEWAY_UI_PORT                 Account UI port (default: 8000).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import uvicorn
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .agent_broker import (
    AgentAttachWorkflow,
    AgentDiscoveryWorkflow,
    publish_agent_events,
)
from .registry import (
    GLOBAL_CATALOG_WORKFLOW_ID,
    REGISTRY_TASK_QUEUE,
    AgentRegistration,
    GlobalCatalogWorkflow,
    ToolRegistryWorkflow,
    account_registry_workflow_id,
    fetch_external_tools,
)
from .registry_service_handler import (
    GatewayA2AServiceHandler,
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


async def _seed_external_servers(
    client: Client, seed: dict[str, str], account_id: str
) -> None:
    """Seed one account's external MCP registrations."""
    registry_handle = await _ensure_account(client, account_id)
    for name, url in seed.items():
        await registry_handle.signal(
            ToolRegistryWorkflow.register_external, args=[name, url]
        )
        logger.info(
            "Seeded external server %r -> %s (account_id=%r)", name, url, account_id
        )


async def _seed_agents(
    client: Client, seed: list[dict[str, object]], account_id: str
) -> None:
    """Compatibility seeding for older deployments; prefer the global catalog."""
    registry_handle = await _ensure_account(client, account_id)
    for item in seed:
        kind = str(item.get("kind", "harness_nexus"))
        registration = AgentRegistration(
            agent_id=str(item["agent_id"]),
            kind=kind,
            label=str(item["label"]),
            description=str(item.get("description", "")),
            nexus_endpoint=(
                str(item["nexus_endpoint"])
                if item.get("nexus_endpoint") is not None
                else None
            ),
            provider_url=(
                str(item["provider_url"])
                if item.get("provider_url") is not None
                else None
            ),
        )
        await registry_handle.execute_update(
            ToolRegistryWorkflow.register_agent,
            registration,
            id=f"seed-agent-{registration.resource_id}",
        )
        logger.info(
            "Seeded agent %r (%s) for account_id=%r",
            registration.resource_id,
            registration.kind,
            account_id,
        )


async def main(
    seed_external_servers: dict[str, str] | None = None,
    seed_account_id: str | None = None,
    seed_agents: list[dict[str, object]] | None = None,
    ui_account_id: str | None = None,
    ui_port: int = 8000,
    catalog_resources: list[ResourceDescriptor] | None = None,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if (seed_external_servers or seed_agents) and not seed_account_id:
        raise SystemExit(
            "GATEWAY_SEED_ACCOUNT_ID is required when a GATEWAY_SEED_* value is set"
        )

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    worker = Worker(
        client,
        task_queue=REGISTRY_TASK_QUEUE,
        workflows=[
            GlobalCatalogWorkflow,
            ToolRegistryWorkflow,
            AgentDiscoveryWorkflow,
            AgentAttachWorkflow,
        ],
        activities=[
            mcp_proxy_activity,
            fetch_external_tools,
            subagent_start_activity,
            subagent_proxy_activity,
            subagent_stop_activity,
            publish_agent_events,
        ],
        nexus_service_handlers=[
            RegistryServiceHandler(client),
            GatewayA2AServiceHandler(client),
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
        if seed_account_id:
            await _ensure_account(client, seed_account_id)
        if seed_external_servers:
            assert seed_account_id is not None
            await _seed_external_servers(client, seed_external_servers, seed_account_id)
        if seed_agents:
            assert seed_account_id is not None
            await _seed_agents(client, seed_agents, seed_account_id)
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
    seed_json = os.environ.get("GATEWAY_SEED_EXTERNAL_SERVERS")
    agent_seed_json = os.environ.get("GATEWAY_SEED_AGENTS")
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
            json.loads(seed_json) if seed_json else None,
            os.environ.get("GATEWAY_SEED_ACCOUNT_ID"),
            json.loads(agent_seed_json) if agent_seed_json else None,
            os.environ.get("GATEWAY_UI_ACCOUNT_ID"),
            int(os.environ.get("GATEWAY_UI_PORT", "8000")),
            catalog_resources,
        )
    )
