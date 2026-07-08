# ABOUTME: The agent directory's actual state — a long-lived workflow instance (one per
# registry deployment, fixed workflow id) holding the registered agents in memory. Mirrors
# nexus/durable_tools_gateway's ToolRegistryWorkflow shape: signals mutate the directory, a
# query reads it. registry_service_handler.py is the only caller.
#
# TTL liveness: register_agent/heartbeat_agent stamp a timestamp; list_agents excludes anything
# older than STALE_AFTER_SECONDS at query time. Staleness never deletes an entry (only explicit
# deregister does), so a late heartbeat un-stales it for free.

from __future__ import annotations

from temporalio import workflow

from agent_registry.agent_registry_service import (
    AgentElement,
)

# Fixed id so every registration/discovery call resolves the SAME running instance — there is
# exactly one agent directory per registry deployment.
AGENT_REGISTRY_WORKFLOW_ID = "agent-registry"

# How long an entry stays visible without a fresh register/heartbeat call (see
# nexus/subagents' registration.py for the expected heartbeat interval).
STALE_AFTER_SECONDS = 90.0


@workflow.defn(name="AgentRegistryWorkflow")
class AgentRegistryWorkflow:
    """The agent directory. Runs forever; entries live only in workflow state (no persistence
    beyond Temporal's own workflow history) — a restart with a fresh workflow id starts an
    empty directory. Fine for a prototype; agents re-register on their own worker restarts."""

    def __init__(self) -> None:
        self._agents: dict[str, AgentElement] = {}
        self._last_seen: dict[str, float] = {}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.signal
    async def register_agent(self, entry: AgentElement) -> None:
        """Add or replace the entry for ``entry.agent_key`` (re-registration overwrites) and
        mark it freshly seen."""
        self._agents[entry.agent_key] = entry
        self._touch(entry.agent_key)

    @workflow.signal
    async def heartbeat_agent(self, agent_key: str) -> None:
        """Mark ``agent_key`` freshly seen. No-op if the key isn't registered."""
        if agent_key in self._agents:
            self._touch(agent_key)

    @workflow.signal
    async def deregister_agent(self, agent_key: str) -> None:
        """Remove an entry. Idempotent — deregistering an unknown key is a no-op."""
        self._agents.pop(agent_key, None)
        self._last_seen.pop(agent_key, None)

    @workflow.query
    def list_agents(self, now: float) -> list[AgentElement]:
        """Current directory, excluding entries not seen within STALE_AFTER_SECONDS.

        ``now`` must come from the caller (real ``time.time()``, in registry_service_handler.py)
        — not computed here, because a query never advances ``workflow.now()``, so a registry
        idle between heartbeats would see it frozen and never expire anything."""
        return [
            entry
            for key, entry in self._agents.items()
            if now - self._last_seen.get(key, 0.0) <= STALE_AFTER_SECONDS
        ]

    def _touch(self, agent_key: str) -> None:
        self._last_seen[agent_key] = workflow.now().timestamp()
