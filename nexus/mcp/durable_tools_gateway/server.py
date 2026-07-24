"""Durable Tool Call Gateway — MCP server entry point.

Serves the InboundGateway over MCP Streamable HTTP so any MCP client
(Gemini SDK, Claude Desktop, etc.) can reach durable tools via a plain HTTP URL.

Usage (from the repo root — this module runs directly against the monorepo's own dependency
graph, no separate package/venv for it; see examples/nexus_hello/justfile's `registry` recipe)::

    uv run --extra nexus-mcp --group examples python -m durable_tools_gateway.server

Environment variables:

    GATEWAY_BIND      Local bind address for uvicorn, e.g. ``http://0.0.0.0:8001``
                      (default, port 8001 to avoid conflicting with the UI
                      server on 8000).  Only affects what this process listens
                      on - not what URL is given to external callers.

    MCP_GATEWAY_URL   The public-facing URL that external callers (Gemini,
                      Claude Desktop, …) use to reach this server, e.g.
                      ``https://abc123.ngrok-free.app`` for a tunnel or
                      ``https://gateway.example.com`` for a deployed instance.
                      Logged at startup so you can copy it into clients.
                      Defaults to ``GATEWAY_BIND`` when not set (useful when
                      the caller is on the same machine, e.g. tests).

    NEXUS_ENDPOINT    Temporal Nexus endpoint name whose task queue hosts the
                      MCP service handlers (default: ``qa-tools-endpoint``,
                      matching the endpoint created by ``just setup-nexus``).

    TEMPORAL_PROFILE / TEMPORAL_CONFIG_FILE
                      Standard Temporal connection config - same values used
                      by the qa_tools worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from urllib.parse import urlparse

import uvicorn
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.utils.large_payload import with_large_payload_offload
from .activities import fetch_external_tools, mcp_proxy_activity
from .inbound import InboundGateway
from .registry import REGISTRY_TASK_QUEUE, REGISTRY_WORKFLOW_ID, ToolRegistryWorkflow
from .registry_service_handler import RegistryServiceHandler
from .tool_call import ToolCallWorkflow

logger = logging.getLogger(__name__)

_DEFAULT_BIND = "http://0.0.0.0:8001"
_DEFAULT_ENDPOINT = "qa-tools-endpoint"

GATEWAY_BIND = os.environ.get("GATEWAY_BIND", _DEFAULT_BIND)
MCP_GATEWAY_URL = os.environ.get("MCP_GATEWAY_URL", GATEWAY_BIND)
NEXUS_ENDPOINT = os.environ.get("NEXUS_ENDPOINT", _DEFAULT_ENDPOINT)

_parsed = urlparse(GATEWAY_BIND)
_HOST = _parsed.hostname or "0.0.0.0"
_PORT = _parsed.port or 8001


class _NormalizeMcpPath:
    """Rewrite ``/mcp`` -> ``/mcp/`` before routing so Starlette's ``Mount``
    doesn't issue a 307 redirect for every request Gemini sends without a
    trailing slash."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path", "") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self._app(scope, receive, send)


async def build_app() -> ASGIApp:
    """Construct the ASGI app.  Called once at startup."""
    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    mcp_server = Server("durable-tool-gateway")
    gateway = InboundGateway(client=client, endpoint=NEXUS_ENDPOINT)
    gateway.register(mcp_server)

    session_manager = StreamableHTTPSessionManager(mcp_server, stateless=True)

    await client.start_workflow(
        ToolRegistryWorkflow.run,
        id=REGISTRY_WORKFLOW_ID,
        task_queue=REGISTRY_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    registry_worker = Worker(
        client,
        task_queue=REGISTRY_TASK_QUEUE,
        workflows=[ToolRegistryWorkflow, ToolCallWorkflow],
        activities=[mcp_proxy_activity, fetch_external_tools],
        nexus_service_handlers=[RegistryServiceHandler(client)],
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):  # type: ignore[type-arg]
        async with registry_worker:
            async with session_manager.run():
                logger.info(
                    "Durable Tool Call Gateway ready  endpoint=%r  bind=%s  public=%s/mcp",
                    NEXUS_ENDPOINT,
                    GATEWAY_BIND,
                    MCP_GATEWAY_URL,
                )
                yield

    starlette_app = Starlette(
        lifespan=lifespan,
        routes=[
            Mount("/mcp/", app=session_manager.handle_request),
            Route("/health", endpoint=lambda _r: Response("ok")),
        ],
    )
    return _NormalizeMcpPath(starlette_app)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    app = await build_app()
    config = uvicorn.Config(app, host=_HOST, port=_PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
