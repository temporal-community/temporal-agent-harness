"""ToolRegistryWorkflow -- routing table for 3rd-party MCP servers, per agent_id.

Native Nexus services never register here -- they're reached directly by the agent
(see nexus_native_mcp_server). This workflow only tracks 3rd-party server URLs, and for
each the name of the credential the gateway sends to it. The secret is never here (see
credentials.py).
No tool content cached: RegistryServiceHandler fetches tools live, on every
list_agent_entries/call_tool call.

Workflow ID:  REGISTRY_WORKFLOW_ID  (singleton per namespace)
Task queue:   "mcp-registry"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow

from .credentials import connect

REGISTRY_WORKFLOW_ID = "mcp-tool-registry"
REGISTRY_TASK_QUEUE = "mcp-registry"


@activity.defn
async def fetch_external_tools(
    name: str, url: str, credential: str | None = None
) -> list[dict[str, Any]]:
    """Fetch an external server's tool list. Prefixes each tool `{name}_{tool}`."""
    activity.logger.info("[registry] fetching tools from %s", url)
    activity.heartbeat()

    async with connect(url, credential) as client:
        result = await client.list_tools()

    tools = []
    for tool in result.tools:
        prefixed = tool.model_copy(update={"name": f"{name}_{tool.name}"})
        tools.append(
            prefixed.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    activity.logger.info("[registry] fetched %d tool(s) from %s", len(tools), url)
    return tools


@dataclass
class AgentEntries:
    """All registered 3rd-party servers for one agent_id. Routing only, no tool content."""

    remote_servers: dict[str, str] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)  # alias -> credential name


@dataclass
class RemoteServer:
    """One registered server: where it is, and the name of its credential, if any."""

    url: str
    credential: str | None = None


@workflow.defn(sandboxed=False, name="ToolRegistry")
class ToolRegistryWorkflow:
    """Perpetual routing-table workflow for the Durable Tool Call Gateway."""

    def __init__(self) -> None:
        self._remote_entries: dict[str, dict[str, str]] = {}  # agent_id -> {alias: url}
        # agent_id -> {alias: credential name}. Beside the URLs, so `find` keeps its shape.
        self._credentials: dict[str, dict[str, str]] = {}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    # -- registration ------------------------------------------------------------

    @workflow.signal
    async def register_external(
        self, agent_id: str, name: str, url: str, credential: str | None = None
    ) -> None:
        """Register a 3rd-party server's URL under one agent_id, with the name of the
        credential to send to it, if it needs one. Validates it's reachable (fetch,
        result discarded) but registers either way -- tools are fetched live later, not
        cached here.
        """
        self._remote_entries.setdefault(agent_id, {})[name] = url
        if credential:
            self._credentials.setdefault(agent_id, {})[name] = credential
        else:
            self._credentials.get(agent_id, {}).pop(name, None)
        try:
            tools = await workflow.execute_activity(
                fetch_external_tools,
                args=[name, url, credential or None],
                start_to_close_timeout=timedelta(seconds=60),
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
        self._credentials.get(agent_id, {}).pop(name, None)
        if self._remote_entries.get(agent_id, {}).pop(name, None) is not None:
            workflow.logger.info("[registry] deregistered %r for agent %r", name, agent_id)

    @workflow.signal
    def clear_all(self) -> None:
        """Remove every registration, for every agent."""
        self._remote_entries.clear()
        self._credentials.clear()
        workflow.logger.info("[registry] cleared all entries")

    # -- queries -----------------------------------------------------------------

    @workflow.query
    def find(self, agent_id: str, name: str) -> str | None:
        """The URL registered for one alias under one agent_id, or None."""
        return self._remote_entries.get(agent_id, {}).get(name)

    @workflow.query
    def find_server(self, agent_id: str, name: str) -> RemoteServer | None:
        """The server registered for one alias under one agent_id, or None."""
        url = self._remote_entries.get(agent_id, {}).get(name)
        if url is None:
            return None
        return RemoteServer(url=url, credential=self._credentials.get(agent_id, {}).get(name))

    @workflow.query
    def list_agent_entries(self, agent_id: str) -> AgentEntries:
        """Routing for one agent_id. No tool content -- fetched live by the caller."""
        return AgentEntries(
            remote_servers=dict(self._remote_entries.get(agent_id, {})),
            credentials=dict(self._credentials.get(agent_id, {})),
        )
