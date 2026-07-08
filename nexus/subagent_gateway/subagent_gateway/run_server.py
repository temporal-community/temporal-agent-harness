"""Run the inbound A2A gateway, fronting one agent looked up in the agent registry.

Connection settings come from a ``temporal.toml`` profile, resolved through temporalio's
``ClientConfig.load_client_connect_config()`` — same convention as agent_registry.run_worker
and examples/monty/worker.py.

Env vars:
    TEMPORAL_CONFIG_FILE           path to a temporal.toml
    TEMPORAL_PROFILE               profile name to load (default: "default")
    AGENT_REGISTRY_NEXUS_ENDPOINT  the agent registry's Nexus endpoint name (required)
    GATEWAY_AGENT_KEY              the agent_key to front, as registered (required)
    GATEWAY_DEFAULT_HANDLER        the handler to route incoming A2A messages to (optional —
                                    required only if the agent has more than one handler)
    GATEWAY_PORT                   port to listen on (default: 8080)
    GATEWAY_PUBLIC_URL             the URL this gateway's own AgentCard advertises as `url`
                                    (default: http://localhost:<port>)
"""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from .app import create_app
from .config import GatewayConfig, resolve_agent


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    registry_endpoint = os.environ["AGENT_REGISTRY_NEXUS_ENDPOINT"]
    agent_key = os.environ["GATEWAY_AGENT_KEY"]
    port = int(os.environ.get("GATEWAY_PORT", "8080"))
    config = GatewayConfig(
        registry_endpoint=registry_endpoint,
        agent_key=agent_key,
        default_handler=os.environ.get("GATEWAY_DEFAULT_HANDLER"),
        port=port,
        public_url=os.environ.get("GATEWAY_PUBLIC_URL", f"http://localhost:{port}"),
    )

    connect_config = ClientConfig.load_client_connect_config()
    client = await Client.connect(**connect_config, data_converter=pydantic_data_converter)

    agent, default_handler = await resolve_agent(client, config)

    app = create_app(client=client, config=config, agent=agent, default_handler=default_handler)
    print(
        f"A2A gateway ready: profile={os.environ.get('TEMPORAL_PROFILE', 'default')!r} "
        f"agent_key={agent_key!r} default_handler={default_handler!r} "
        f"nexus_endpoint={agent.endpoint!r} listening on :{port}",
        flush=True,
    )

    server_config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    await uvicorn.Server(server_config).serve()


if __name__ == "__main__":
    asyncio.run(main())
