# ABOUTME: The agent directory's actual state — a long-lived workflow instance (one per
# registry deployment, fixed workflow id) holding the registered agents in memory. Mirrors
# nexus/durable_tools_gateway's ToolRegistryWorkflow shape: signals mutate the directory, a
# query reads it. registry_service_handler.py is the only caller.

from __future__ import annotations

from temporalio import workflow

from agent_registry.agent_registry_service import (
    AgentElement,
)

# Fixed id so every registration/discovery call resolves the SAME running instance — there is
# exactly one agent directory per registry deployment.
AGENT_REGISTRY_WORKFLOW_ID = "agent-registry"


@workflow.defn(name="AgentRegistryWorkflow")
class AgentRegistryWorkflow:
    """The agent directory. Runs forever; entries live only in workflow state (no persistence
    beyond Temporal's own workflow history) — a restart with a fresh workflow id starts an
    empty directory. Fine for a prototype; agents re-register on their own worker restarts."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentElement] = {}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.signal
    async def register_agent(self, entry: AgentElement) -> None:
        """Add or replace the entry for ``entry.agent_key`` (re-registration overwrites)."""
        self._agents[entry.agent_key] = entry

    @workflow.signal
    async def deregister_agent(self, agent_key: str) -> None:
        """Remove an entry. Idempotent — deregistering an unknown key is a no-op."""
        self._agents.pop(agent_key, None)

    @workflow.query
    def list_agents(self) -> list[AgentElement]:
        """The full current directory, in registration order."""
        return list(self._agents.values())
