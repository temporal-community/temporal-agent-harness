"""Routing workflow for registered HTTP MCP servers and subagents.

Native Nexus resources do not use this registry. MCP tool definitions are fetched on each
list request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

REGISTRY_WORKFLOW_ID = "mcp-tool-registry"
REGISTRY_TASK_QUEUE = "mcp-registry"


@activity.defn
async def fetch_external_tools(name: str, url: str) -> list[dict[str, Any]]:
    """Fetch an external server's tool list. Prefixes each tool `{name}_{tool}`."""
    activity.logger.info("[registry] fetching tools from %s", url)
    activity.heartbeat()

    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()

    tools = []
    for tool in result.tools:
        prefixed = tool.model_copy(update={"name": f"{name}_{tool.name}"})
        tools.append(prefixed.model_dump())

    activity.logger.info("[registry] fetched %d tool(s) from %s", len(tools), url)
    return tools


@dataclass
class AgentEntries:
    """All registered 3rd-party servers for one agent_id. Routing only, no tool content."""

    remote_servers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SubagentInstanceRoute:
    """Route for one instance created by a registered factory."""

    alias: str
    url: str
    provider_instance_id: str


@workflow.defn(sandboxed=False, name="ToolRegistry")
class ToolRegistryWorkflow:
    """Perpetual routing-table workflow for the Durable Tool Call Gateway."""

    def __init__(self) -> None:
        self._remote_entries: dict[str, dict[str, str]] = {}  # agent ID -> alias -> URL
        self._subagent_entries: dict[str, dict[str, str]] = {}  # agent ID -> alias -> URL
        self._subagent_instances: dict[
            str, dict[str, SubagentInstanceRoute]
        ] = {}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    # -- registration ------------------------------------------------------------

    @workflow.signal
    async def register_external(self, agent_id: str, name: str, url: str) -> None:
        """Register an MCP server and check that it is reachable."""
        self._remote_entries.setdefault(agent_id, {})[name] = url
        try:
            tools = await workflow.execute_activity(
                fetch_external_tools,
                args=[name, url],
                schedule_to_close_timeout=timedelta(seconds=60),
                start_to_close_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except Exception as exc:
            workflow.logger.warning(
                "[registry] registered %r at %s, validation fetch failed: %s", name, url, exc
            )
            return
        workflow.logger.info("[registry] registered %r at %s (%d tools)", name, url, len(tools))

    @workflow.signal
    def deregister(self, agent_id: str, name: str) -> None:
        """Remove one registration under one agent_id."""
        if self._remote_entries.get(agent_id, {}).pop(name, None) is not None:
            workflow.logger.info("[registry] deregistered %r for agent %r", name, agent_id)

    @workflow.signal
    def clear_all(self) -> None:
        """Remove all MCP server and subagent registrations."""
        self._remote_entries.clear()
        self._subagent_entries.clear()
        workflow.logger.info("[registry] cleared all entries")

    @workflow.signal
    def register_subagent(self, agent_id: str, alias: str, url: str) -> None:
        """Register a non-Nexus subagent's URL under one agent_id."""
        self._subagent_entries.setdefault(agent_id, {})[alias] = url
        workflow.logger.info("[registry] registered subagent %r at %s", alias, url)

    @workflow.signal
    def deregister_subagent(self, agent_id: str, alias: str) -> None:
        """Remove one subagent registration under one agent_id."""
        if self._subagent_entries.get(agent_id, {}).pop(alias, None) is not None:
            workflow.logger.info(
                "[registry] deregistered subagent %r for agent %r", alias, agent_id
            )

    @workflow.update
    def bind_subagent_instance(
        self,
        agent_id: str,
        instance_id: str,
        route: SubagentInstanceRoute,
    ) -> None:
        """Bind a gateway task to one provider route.

        A2A starts third-party tasks lazily: the first binding knows the alias and
        URL, while the provider task ID arrives with the first response.  Permit
        that one-way promotion, and make a retried provisional bind a no-op once
        the provider ID is known.  Neither case may change the selected provider.
        """
        routes = self._subagent_instances.setdefault(agent_id, {})
        existing = routes.get(instance_id)
        if existing is not None:
            same_provider = existing.alias == route.alias and existing.url == route.url
            provider_id_is_compatible = (
                existing.provider_instance_id == route.provider_instance_id
                or not existing.provider_instance_id
                or not route.provider_instance_id
            )
            if not same_provider or not provider_id_is_compatible:
                raise ApplicationError(
                    f"subagent instance {instance_id!r} has a different route",
                    type="SubagentInstanceConflict",
                    non_retryable=True,
                )
            if existing.provider_instance_id and not route.provider_instance_id:
                return
        routes[instance_id] = route

    @workflow.update
    def unbind_subagent_instance(self, agent_id: str, instance_id: str) -> None:
        """Remove one gateway instance route."""
        self._subagent_instances.get(agent_id, {}).pop(instance_id, None)

    # -- queries -----------------------------------------------------------------

    @workflow.query
    def find(self, agent_id: str, name: str) -> str | None:
        """The URL registered for one MCP-server alias under one agent_id, or None."""
        return self._remote_entries.get(agent_id, {}).get(name)

    @workflow.query
    def find_subagent(self, agent_id: str, alias: str) -> str | None:
        """The URL registered for one subagent alias under one agent_id, or None."""
        return self._subagent_entries.get(agent_id, {}).get(alias)

    @workflow.query
    def find_subagent_instance(
        self, agent_id: str, instance_id: str
    ) -> SubagentInstanceRoute | None:
        """Return the provider route for one gateway instance."""
        return self._subagent_instances.get(agent_id, {}).get(instance_id)

    @workflow.query
    def list_agent_entries(self, agent_id: str) -> AgentEntries:
        """Routing for one agent_id. No tool content -- fetched live by the caller."""
        return AgentEntries(remote_servers=dict(self._remote_entries.get(agent_id, {})))
