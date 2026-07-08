# ABOUTME: GatewayConfig + the standalone agent-registry lookup that resolves it into a live
# AgentElement — reuses the same registry agent workers self-register into, so the gateway needs
# no separate config for the fronted agent's endpoint/capability. One gateway = one agent_key.

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import Client

from subagents.registry.agent_registry_service import (
    AgentElement,
    AgentRegistryService,
    DiscoverAgentsInput,
)

_OPERATION_TIMEOUT = timedelta(seconds=30)


@dataclass
class GatewayConfig:
    """Fixed per-deployment config. The fronted agent's endpoint/capability is resolved live
    from the registry at startup (:func:`resolve_agent`), not configured here."""

    registry_endpoint: str
    agent_key: str
    # A2A messages have no handler-selection concept, so this gateway always routes to one
    # handler. Left unset, resolve_agent() picks the sole handler if there's exactly one.
    default_handler: str | None = None
    port: int = 8080
    # What this gateway's own AgentCard advertises as `url` — external callers dial this.
    public_url: str = "http://localhost:8080"


async def resolve_agent(client: Client, config: GatewayConfig) -> tuple[AgentElement, str]:
    """Look up ``config.agent_key`` in the registry; return it with the resolved default handler.

    Raises ``LookupError`` if the key isn't registered, or if ``default_handler`` is unset and
    the agent doesn't have exactly one handler to fall back to."""
    registry = client.create_nexus_client(
        service=AgentRegistryService, endpoint=config.registry_endpoint
    )
    result = await registry.execute_operation(
        AgentRegistryService.discover_agents,
        DiscoverAgentsInput(),
        id=f"gateway-resolve-{config.agent_key}",
        schedule_to_close_timeout=_OPERATION_TIMEOUT,
    )
    for agent in result.agents:
        if agent.agent_key == config.agent_key:
            handler = config.default_handler
            if handler is None:
                if len(agent.handlers) != 1:
                    raise LookupError(
                        f"agent {config.agent_key!r} has {len(agent.handlers)} handlers "
                        f"({sorted(h.name for h in agent.handlers)}); GatewayConfig.default_handler "
                        "must be set explicitly when there isn't exactly one."
                    )
                handler = agent.handlers[0].name
            return agent, handler
    raise LookupError(
        f"agent {config.agent_key!r} is not currently registered with the agent registry "
        f"at {config.registry_endpoint!r}. Currently registered: "
        f"{sorted(a.agent_key for a in result.agents)}."
    )
