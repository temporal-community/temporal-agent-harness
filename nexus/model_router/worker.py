"""Standalone worker hosting the model router Nexus service.

Run from the repo root with:
    uv run --group examples python -m nexus.model_router.worker

Registers ``ModelRouterServiceHandler`` on the ``model-router`` task queue and
creates the Nexus endpoint (idempotent) that maps ``NEXUS_ENDPOINT`` -> this
worker. Any workflow (in this namespace) can then call the router over Nexus.

Uses the pydantic data converter so the router's dataclass request and the
OpenAI ``ChatCompletion`` response serialize cleanly — and compatibly with the
OpenAI Agents plugin's (also pydantic-based) converter on the caller side.

Env:
    OPENAI_API_KEY                             required — the router calls OpenAI.
    TEMPORAL_CONFIG_FILE / TEMPORAL_PROFILE    connection profile (else localhost:7233).
    TEMPORAL_ADDRESS / TEMPORAL_NAMESPACE      fallback connection when no profile.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import temporalio.api.nexus.v1 as nexus_pb
import temporalio.api.operatorservice.v1 as operator_pb
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from .handler import ModelRouterServiceHandler
from .service import NEXUS_ENDPOINT, TASK_QUEUE


def _connect_config() -> dict[str, Any]:
    try:
        cfg = ClientConfig.load_client_connect_config()
        if cfg.get("target_host"):
            return cfg
    except Exception:
        pass
    return {
        "target_host": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        "namespace": os.environ.get("TEMPORAL_NAMESPACE", "default"),
    }


async def ensure_endpoint(client: Client, namespace: str) -> None:
    """Create the Nexus endpoint (idempotent) mapping NEXUS_ENDPOINT -> TASK_QUEUE.

    Equivalent CLI:
        temporal operator nexus endpoint create --name model-router-endpoint \\
            --target-namespace <ns> --target-task-queue model-router
    """
    spec = nexus_pb.EndpointSpec(
        name=NEXUS_ENDPOINT,
        target=nexus_pb.EndpointTarget(
            worker=nexus_pb.EndpointTarget.Worker(
                namespace=namespace,
                task_queue=TASK_QUEUE,
            )
        ),
    )
    try:
        await client.operator_service.create_nexus_endpoint(
            operator_pb.CreateNexusEndpointRequest(spec=spec)
        )
        print(f"created nexus endpoint {NEXUS_ENDPOINT!r} -> {namespace}/{TASK_QUEUE}")
    except RPCError as e:
        if e.status == RPCStatusCode.ALREADY_EXISTS:
            print(f"nexus endpoint {NEXUS_ENDPOINT!r} already exists")
        else:
            raise


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("error: OPENAI_API_KEY env var not set")

    cfg = _connect_config()
    client = await Client.connect(**cfg, data_converter=pydantic_data_converter)

    namespace = cfg.get("namespace") or client.namespace
    await ensure_endpoint(client, namespace)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        nexus_service_handlers=[ModelRouterServiceHandler()],
    )
    print(
        f"model-router worker ready: address={cfg.get('target_host')} "
        f"namespace={namespace} taskQueue={TASK_QUEUE} nexusEndpoint={NEXUS_ENDPOINT}",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
