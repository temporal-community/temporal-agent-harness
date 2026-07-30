"""Durable Tool Call Gateway Temporal worker. This serves:
- the handler for the registry service, where we can register 3rd-party MCP servers
- the durable tool call flow, which wraps tool calls around an activity that does the actual HTTP call to the 3rd-party MCP server
TODO: convert the ToolCallWorkflow to an SAA once we have SDK support for Nexus-invoked SAA.

Usage (from repo root):
    uv run --extra nexus-mcp --group examples python -m durable_tools_gateway.worker

Env vars:
    GATEWAY_SEED_EXTERNAL_SERVERS   Optional JSON object, {"name": "url", ...}, of 3rd-party
                                     external MCP servers to register_external on startup --
                                     lets a deployment prepopulate known servers instead of
                                     requiring a separate manual RegisterExternal call per
                                     server. Unset/empty: seeds nothing (default).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .registry import (
    REGISTRY_TASK_QUEUE,
    REGISTRY_WORKFLOW_ID,
    RegisterExternalWorkflow,
    RegisterExternalWorkflowInput,
    ToolRegistryWorkflow,
    fetch_external_tools,
)
from .registry_service_handler import (
    RegistryServiceHandler,
    ToolCallWorkflow,
    mcp_proxy_activity,
)

logger = logging.getLogger(__name__)


async def _seed_external_servers(client: Client, seed: dict[str, str]) -> None:
    """Register each {name: url} pair via the same durable fetch-then-register flow
    RegistryServiceHandler.register_external uses -- so a not-yet-reachable server (e.g.
    still starting up) is absorbed by that flow's own activity retries rather than failing
    the seed outright. A server that never becomes reachable is logged and skipped; the
    gateway itself still starts fine either way."""
    registry_handle = client.get_workflow_handle(REGISTRY_WORKFLOW_ID)
    for name, url in seed.items():
        try:
            fetch_handle = await client.start_workflow(
                RegisterExternalWorkflow.run,
                RegisterExternalWorkflowInput(name=name, url=url),
                id=f"mcp-register-external-{name}-seed",
                task_queue=REGISTRY_TASK_QUEUE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
            tools = await fetch_handle.result()
        except Exception as exc:
            logger.warning("Could not seed external server %r at %s: %s", name, url, exc)
            continue
        await registry_handle.signal(
            ToolRegistryWorkflow.register_external, args=[name, url, tools]
        )
        logger.info("Seeded external server %r -> %s", name, url)


async def main(seed_external_servers: dict[str, str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
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
        workflows=[ToolRegistryWorkflow, ToolCallWorkflow, RegisterExternalWorkflow],
        activities=[mcp_proxy_activity, fetch_external_tools],
        nexus_service_handlers=[RegistryServiceHandler(client)],
    )
    async with worker:
        logger.info("Durable Tool Call Gateway ready — task_queue=%r", REGISTRY_TASK_QUEUE)
        if seed_external_servers:
            await _seed_external_servers(client, seed_external_servers)
        await asyncio.Event().wait()


if __name__ == "__main__":
    seed_json = os.environ.get("GATEWAY_SEED_EXTERNAL_SERVERS")
    asyncio.run(main(json.loads(seed_json) if seed_json else None))
