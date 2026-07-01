"""ToolRegistryWorkflow - routing table for the Durable Tool Call Gateway.

A perpetual Temporal workflow (run once, lives forever) that maps service names
to either a 1st-party Nexus service or a 3rd-party external MCP server URL.
The InboundGateway queries it on every call to decide routing.

Workflow ID:  REGISTRY_WORKFLOW_ID  (singleton per Temporal namespace)
Task queue:   "mcp-registry"

Signal / query handlers
------------------------
  register_nexus(name, endpoint, tools)   add / update a 1st-party Nexus service
  register_external(name, url)            queue a 3rd-party server for tool fetching
  deregister(name)                        remove any entry by name
  clear_all()                             remove all entries
  find(name) -> RegistryEntry | None      look up routing for one service
  list_all_tools() -> list[dict]          all tool dicts — nexus + external
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
    """Routing entry stored for one service name."""

    kind: str
    """``"nexus"`` for 1st-party Nexus services, ``"external"`` for 3rd-party
    MCP servers reached over HTTP."""

    # nexus only
    endpoint: str = ""
    """Temporal Nexus endpoint name to route this service's calls through."""

    # external only
    url: str = ""
    """Streamable-HTTP MCP endpoint URL for external servers."""

    # both
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
                self._entries[name] = RegistryEntry(
                    kind="external", url=url, tools=tools
                )
                tool_names = [t.get("name", "?") for t in tools]
                print(
                    f"[registry] Successfully registered external MCP server {name!r} at {url}  "
                    f"({len(tools)} tools: {tool_names})",
                    flush=True,
                )

    # -- registration ------------------------------------------------------------

    @workflow.signal
    def register_nexus(
        self, name: str, endpoint: str, tools: list[dict[str, Any]]
    ) -> None:
        """Add or replace a 1st-party Nexus service registration."""
        tool_names = [t.get("name", "?") for t in tools]
        self._entries[name] = RegistryEntry(
            kind="nexus", endpoint=endpoint, tools=tools
        )
        print(
            f"[registry] Successfully registered Nexus MCP server {name!r} at {endpoint}  "
            f"({len(tools)} tools: {tool_names})",
            flush=True,
        )

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
    def list_all_tools(self) -> list[dict[str, Any]]:
        """Return all tool dicts — both 1st-party Nexus and 3rd-party external."""
        result: list[dict[str, Any]] = []
        for entry in self._entries.values():
            result.extend(entry.tools)
        return result
