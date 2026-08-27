"""ToolRegistryWorkflow -- routing table for 3rd-party MCP servers, per agent_id.

Native Nexus services never register here -- they're reached directly by the agent
(see nexus_native_mcp_server). This workflow only tracks 3rd-party server URLs.
No tool content cached: RegistryServiceHandler fetches tools live, on every
list_agent_entries/call_tool call.

Workflow ID:  REGISTRY_WORKFLOW_ID  (singleton per namespace)
Task queue:   "mcp-registry"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from temporalio import activity, workflow

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


@workflow.defn(sandboxed=False, name="ToolRegistry")
class ToolRegistryWorkflow:
    """Perpetual routing-table workflow for the Durable Tool Call Gateway."""

    def __init__(self) -> None:
        self._remote_entries: dict[str, dict[str, str]] = {}  # agent_id -> {alias: url}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    # -- registration ------------------------------------------------------------

    @workflow.signal
    async def register_external(self, agent_id: str, name: str, url: str) -> None:
        """Register a 3rd-party server's URL under one agent_id. Validates it's
        reachable (fetch, result discarded) but registers either way -- tools are
        fetched live later, not cached here.
        """
        self._remote_entries.setdefault(agent_id, {})[name] = url
        try:
            tools = await workflow.execute_activity(
                fetch_external_tools, args=[name, url], start_to_close_timeout=timedelta(seconds=60)
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
        """Remove every registration, for every agent."""
        self._remote_entries.clear()
        workflow.logger.info("[registry] cleared all entries")

    # -- queries -----------------------------------------------------------------

    @workflow.query
    def find(self, agent_id: str, name: str) -> str | None:
        """The URL registered for one alias under one agent_id, or None."""
        return self._remote_entries.get(agent_id, {}).get(name)

    @workflow.query
    def list_agent_entries(self, agent_id: str) -> AgentEntries:
        """Routing for one agent_id. No tool content -- fetched live by the caller."""
        return AgentEntries(remote_servers=dict(self._remote_entries.get(agent_id, {})))
