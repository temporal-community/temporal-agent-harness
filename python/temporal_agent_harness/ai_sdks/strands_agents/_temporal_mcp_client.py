from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from strands.tools import ToolProvider
from strands.tools.mcp import ToolFilters
from strands.types.tools import AgentTool

from temporalio.common import Priority, RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import (
    ActivityCancellationType,
    ActivityConfig,
    VersioningIntent,
)

from temporal_agent_harness.ai_sdks.strands_agents._temporal_mcp_tool import TemporalMCPTool

if TYPE_CHECKING:
    from temporalio.mcp import TemporalMCPClient as DurableMCPClient


class MCPToolNameConflictError(  # pyright: ignore[reportUnsafeMultipleInheritance]
    ApplicationError, ValueError
):
    """An MCP tool has the name of another tool of the same agent.

    ``TemporalAgent`` raises it before a model call when a listed tool's name
    is taken, or differs from a taken name only by ``-`` or ``_``. It is a
    non-retryable ``ApplicationError``, so it fails the Workflow when it escapes
    the Workflow's run method or a Signal handler, instead of retrying the
    Workflow Task, whether ``StrandsPlugin`` is on the client or only on the
    Worker. Escaping an Update handler, it fails only that Update.
    """

    def __init__(self, message: str) -> None:
        """Initialize the error with Strands' message and the tools' sources."""
        super().__init__(message, type="MCPToolNameConflictError", non_retryable=True)
        # ApplicationError puts the type before the message in str(); keep the
        # message as Strands words it.
        self.args = (message,)


class TemporalMCPClient(ToolProvider):
    """Workflow-side handle to an MCP server registered on the worker.

    The ``mcp.Client`` factory lives worker-side via
    ``StrandsPlugin(mcp_clients={"server": lambda: Client(...)})``. This handle
    carries the server name (which selects the registered factory), the tool
    ``prefix`` and ``tool_filters``, and the per-call activity options. MCP
    operations run as the ``temporalio.mcp.{server}.list-tools`` and
    ``temporalio.mcp.{server}.call-tool`` activities from ``temporalio-mcp``.
    Tool discovery is dispatched from inside the workflow by ``TemporalAgent``
    before each model call.

    ``cache_tools`` controls how often that listing happens. When ``False``
    (the default) the tools are re-listed on every agent turn, so an MCP server
    restarted mid-workflow (with tools added, removed, or renamed) is picked up.
    When ``True`` the tools are listed once at the beginning of the workflow and
    reused for its lifetime. Each listing records the server's full tool list
    in the workflow history, before ``tool_filters`` apply, so for a server
    with many tools prefer ``cache_tools=True``.

    ``prefix`` and ``tool_filters`` work as on Strands' ``MCPClient``: the agent
    sees ``{prefix}_{name}``, and filters match the server's tool name. A
    callable filter receives the workflow-side ``TemporalMCPTool``, a Strands
    ``MCPAgentTool`` whose ``mcp_client`` is the durable client, and runs in the
    workflow, so it must be deterministic.

    The activity options apply to every MCP activity. With neither
    ``start_to_close_timeout`` nor ``schedule_to_close_timeout``, a one-minute
    ``start_to_close_timeout`` applies. The default retry policy retries a
    failed activity, such as one whose MCP server is down, without limit; set
    ``retry_policy`` or ``schedule_to_close_timeout`` to bound it.

    Construct once at module level and pass to ``TemporalAgent(tools=[...])``
    inside the workflow. Multiple handles may reference the same server name
    with different activity options or prefixes. On one agent, two handles of
    a server must not list the same name: give them different prefixes or
    disjoint ``tool_filters``.
    """

    def __init__(
        self,
        server: str,
        *,
        cache_tools: bool = False,
        prefix: str | None = None,
        tool_filters: ToolFilters | None = None,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        versioning_intent: VersioningIntent | None = None,
        summary: str | None = None,
        priority: Priority = Priority.default,
    ) -> None:
        """Configure the server name, tool naming and filtering, and activity options."""
        try:
            from temporalio.mcp import TemporalMCPClient as DurableMCPClient
        except ImportError as err:
            # Harness: name the harness extra that carries the MCP dependencies.
            raise RuntimeError(
                "TemporalMCPClient requires MCP Python SDK v2 and temporalio-mcp; "
                "install temporal-agent-harness[strands-agents]"
            ) from err
        self._server = server
        self._cache_tools = cache_tools
        self._prefix = prefix
        self._tool_filters = tool_filters
        self._tools: list[AgentTool] = []
        self._fetched = False
        self._client: DurableMCPClient = DurableMCPClient(
            server,
            activity_config=ActivityConfig(
                task_queue=task_queue,
                schedule_to_close_timeout=schedule_to_close_timeout,
                schedule_to_start_timeout=schedule_to_start_timeout,
                start_to_close_timeout=start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                retry_policy=retry_policy,
                cancellation_type=cancellation_type,
                versioning_intent=versioning_intent,
                summary=summary,
                priority=priority,
            ),
            # TemporalAgent stops calling _refresh once a cached listing is fetched.
            cache_tools_list=False,
        )

    @property
    def server(self) -> str:
        """MCP server name registered on the worker."""
        return self._server

    async def load_tools(self, **_kwargs: Any) -> Sequence[AgentTool]:
        """Return no tools; ``TemporalAgent`` adds them before each model call.

        Strands invokes this once at ``Agent`` construction on a separate
        ``run_async`` thread that has no workflow runtime, so the tools cannot
        be listed here. ``TemporalAgent`` adds them from a
        ``BeforeModelCallEvent`` hook, which lists them by calling ``_refresh``
        and checks their names, for an agent built before or after a listing.
        Until its first model call, an agent has none of these tools.
        """
        return []

    async def _refresh(self) -> None:
        """List the server's tools via the ``list-tools`` activity.

        Runs on the workflow event loop (dispatched from ``TemporalAgent``'s
        hook), so the activity result is recorded in history and replay-safe.
        """
        listed = await self._client.list_tools()
        tools = [
            TemporalMCPTool(self._client, tool, self._prefix) for tool in listed.tools
        ]
        self._tools = [tool for tool in tools if _included(tool, self._tool_filters)]
        self._fetched = True

    def add_consumer(self, consumer_id: Any, **_kwargs: Any) -> None:
        """No-op; consumer tracking is handled by the underlying MCP client."""
        return None

    def remove_consumer(self, consumer_id: Any, **_kwargs: Any) -> None:
        """No-op; consumer tracking is handled by the underlying MCP client."""
        return None


def _included(tool: TemporalMCPTool, filters: ToolFilters | None) -> bool:
    """Apply ``tool_filters`` the way Strands' ``MCPClient`` does."""
    if not filters:
        return True
    if "allowed" in filters and not _matches(tool, filters["allowed"]):
        return False
    return not ("rejected" in filters and _matches(tool, filters["rejected"]))


def _matches(tool: TemporalMCPTool, patterns: Sequence[Any]) -> bool:
    # Same order as Strands; like Strands, it ignores other entries.
    name = tool.mcp_tool.name
    for pattern in patterns:
        if callable(pattern):
            if pattern(tool):
                return True
        elif isinstance(pattern, re.Pattern):
            if cast("re.Pattern[str]", pattern).match(name):
                return True
        elif isinstance(pattern, str):
            if pattern == name:
                return True
    return False
