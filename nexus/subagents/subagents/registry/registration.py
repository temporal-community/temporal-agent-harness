# ABOUTME: Helper for an agent's own worker to register/deregister itself with the agent
# registry at startup/shutdown - mirrors the tool registry's inline registration dance in
# forum_proxy_worker.py (deregister-then-register on boot, deregister again on shutdown),
# factored into two small functions rather than duplicated inline at every call site.
#
# SERVER REQUIREMENT: both functions call `client.create_nexus_client(...)` — a CLIENT-side
# (not workflow-side) Nexus operation, i.e. a "standalone" Nexus call with no workflow driving
# it. That's a newer server capability not yet in a released Temporal server build; the target
# server needs `nexusoperation.enableStandalone` (+ `activity.enableStandalone`) dynamic config
# and a server binary built from `main` — see nexus/devserver/dynamicconfig/development.yaml
# and `make -C nexus/slack_connector install-dev-server`. A stock `temporal server start-dev`
# fails with "unknown method StartNexusOperationExecution". The workflow-side counterparts
# (``discover_registry_agents``/``start_subagent_from_registry`` in ``discovery.py``, which use
# ``workflow.create_nexus_client`` from inside a running workflow) have no such requirement —
# only this module's client-side register/deregister calls do. Covered end-to-end by
# nexus/tests/test_agent_registry_nexus.py.

from __future__ import annotations

from datetime import timedelta

from temporalio.client import Client

from .agent_registry_service import (
    AgentRegistryService,
    DeregisterAgentInput,
    HandlerElement,
    RegisterAgentInput,
)

_OPERATION_TIMEOUT = timedelta(seconds=30)


def _handlers_for(agent_cls: type) -> list[HandlerElement]:
    """The agent's model-callable ``@agent.accepts`` handlers, tool-style — the same reflection
    ``subagent_toolset()`` already does statically, converted to the registry's own wire type
    (structurally identical to the harness's ``AcceptedFunction``, but a distinct generated
    class — see the ``agent_registry`` package).

    Reaches into a harness-private symbol (``_SLASH_MESSAGE_TYPE``) — that's always been true
    of this reflection, even before this lived in its own installable package."""
    from temporal_agent_harness.harness.agent_workflow import (
        _SLASH_MESSAGE_TYPE,
        agent_handlers,
    )

    return [
        HandlerElement(
            name=handler.name,
            description=handler.description,
            parameters=handler.input_type.model_json_schema(),
            output=handler.output_type.model_json_schema(),
        )
        for name, handler in agent_handlers(agent_cls).items()
        if name != _SLASH_MESSAGE_TYPE
    ]


async def register_agent_with_registry(
    client: Client,
    agent_cls: type,
    *,
    agent_key: str,
    endpoint: str,
    registry_endpoint: str,
    description: str = "",
) -> None:
    """Announce ``agent_cls`` to the agent registry: deregister then register, so a worker
    restart re-announces cleanly instead of erroring on an already-registered key."""
    registry = client.create_nexus_client(
        service=AgentRegistryService, endpoint=registry_endpoint
    )
    await registry.execute_operation(
        AgentRegistryService.deregister_agent,
        DeregisterAgentInput(agent_key=agent_key),
        id=f"dereg-{agent_key}",
        schedule_to_close_timeout=_OPERATION_TIMEOUT,
    )
    await registry.execute_operation(
        AgentRegistryService.register_agent,
        RegisterAgentInput(
            agent_key=agent_key,
            endpoint=endpoint,
            handlers=_handlers_for(agent_cls),
            description=description,
        ),
        id=f"reg-{agent_key}",
        schedule_to_close_timeout=_OPERATION_TIMEOUT,
    )


async def deregister_agent_from_registry(
    client: Client, *, agent_key: str, registry_endpoint: str
) -> None:
    """Remove ``agent_key`` from the registry — call this in a ``finally`` around the agent's
    ``worker.run()``, mirroring the tool registry's shutdown deregistration."""
    registry = client.create_nexus_client(
        service=AgentRegistryService, endpoint=registry_endpoint
    )
    await registry.execute_operation(
        AgentRegistryService.deregister_agent,
        DeregisterAgentInput(agent_key=agent_key),
        id=f"dereg-shutdown-{agent_key}",
        schedule_to_close_timeout=_OPERATION_TIMEOUT,
    )
