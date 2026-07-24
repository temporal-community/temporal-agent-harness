"""ToolRegistryWorkflow - routing table for the Durable Tool Call Gateway.

A perpetual Temporal workflow (run once, lives forever) that maps 3rd-party
external MCP server names to their URL and tool list. Nexus-native MCP
servers never appear here — they register directly against the calling
agent's own in-workflow registry (see
``temporal_agent_harness.ai_sdks.openai_agents``'s ``NexusMcpServerRegistry``)
and are called directly, bypassing the gateway entirely. The
InboundGateway/RegistryServiceHandler query this workflow to decide routing
for the 3rd-party servers that remain.

Workflow ID:  REGISTRY_WORKFLOW_ID  (singleton per Temporal namespace)
Task queue:   "mcp-registry"

Signal / query handlers
------------------------
  register_external(name, url)      queue a 3rd-party server for tool fetching
  deregister(name)                  remove any entry by name
  clear_all()                       remove all entries
  find(name) -> RegistryEntry | None look up routing for one service
  list_tools() -> list[dict]        all tool dicts for registered servers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from .activities import fetch_external_tools

REGISTRY_WORKFLOW_ID = "mcp-tool-registry"
REGISTRY_TASK_QUEUE = "mcp-registry"


@dataclass
class RegistryEntry:
    """Routing entry stored for one 3rd-party external MCP server."""

    url: str = ""
    """Streamable-HTTP MCP endpoint URL."""

    tools: list[dict[str, Any]] = field(default_factory=list)
    """Serialised ``mcp.types.Tool`` dicts (name already prefixed with
    ``{service_name}_``)."""


@workflow.defn(sandboxed=False, name="ToolRegistry")
class ToolRegistryWorkflow:
    """Perpetual routing-table workflow for the Durable Tool Call Gateway.

    Start once via ``just setup-nexus`` (or automatically on first worker
    startup).  Never completes — holds state for the lifetime of the namespace.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RegistryEntry] = {}
        self._pending_external: list[tuple[str, str]] = []

    @workflow.run
    async def run(self) -> None:
        while True:
            await workflow.wait_condition(lambda: bool(self._pending_external))
            while self._pending_external:
                name, url = self._pending_external.pop(0)
                try:
                    tools: list[dict[str, Any]] = await workflow.execute_activity(
                        fetch_external_tools,
                        args=[name, url],
                        start_to_close_timeout=timedelta(seconds=60),
                    )
                except ActivityError as exc:
                    workflow.logger.error(
                        "[registry] Failed registering external MCP server %r: could not fetch tools: %s", name, exc
                    )
                    continue
                self._entries[name] = RegistryEntry(url=url, tools=tools)
                tool_names = [t.get("name", "?") for t in tools]
                print(
                    f"[registry] Successfully registered external MCP server {name!r} at {url}  "
                    f"({len(tools)} tools: {tool_names})",
                    flush=True,
                )

    # -- registration ------------------------------------------------------------

    @workflow.signal
    def register_external(self, name: str, url: str) -> None:
        """Queue a 3rd-party MCP server for tool fetching.

        The workflow will call ``fetch_external_tools`` activity to fetch the
        tool list from *url* and store the result.  Callers do not need to
        pre-fetch tools — just provide the service name and URL.
        """
        print(f"[registry] queued external {name!r} -> {url}", flush=True)
        self._pending_external.append((name, url))

    @workflow.signal
    def deregister(self, name: str) -> None:
        """Remove a registration by service name."""
        removed = self._entries.pop(name, None)
        if removed:
            workflow.logger.info("[registry] deregistered %r", name)
        else:
            workflow.logger.debug(
                "[registry] deregister: %r not found (stale signal, ignoring)", name
            )

    @workflow.signal
    def clear_all(self) -> None:
        """Remove all entries and cancel any pending external fetches."""
        count = len(self._entries)
        self._entries.clear()
        self._pending_external.clear()
        workflow.logger.info("[registry] cleared %d entries", count)

    # -- queries -----------------------------------------------------------------

    @workflow.query
    def find(self, name: str) -> RegistryEntry | None:
        """Return the routing entry for *name*, or ``None`` if not registered."""
        return self._entries.get(name)

    @workflow.query
    def list_tools(self) -> list[dict[str, Any]]:
        """Return all tool dicts for registered 3rd-party external servers."""
        result: list[dict[str, Any]] = []
        for entry in self._entries.values():
            result.extend(entry.tools)
        return result
