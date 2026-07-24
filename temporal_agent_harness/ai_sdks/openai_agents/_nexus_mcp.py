"""Private implementation of the Nexus-transport MCP server.

Access it through ``temporal_agent_harness.ai_sdks.openai_agents.workflow.
nexus_transport_mcp_server`` — this module is an implementation detail, not public API.

Unlike ``StatelessMCPServerProvider`` / ``StatefulMCPServerProvider`` (which run the MCP
server's own ``connect``/``list_tools``/``call_tool`` inside an activity, or on a dedicated
worker), this strategy calls straight into ``nexus_mcp``'s ``WorkflowTransport`` in-process,
in the workflow's own event loop — no MCP protocol round trip, no session, nothing to connect.
Every tool call goes through ``workflow.create_nexus_client().execute_operation()``,
uniformly — directly against a Nexus-native MCP server registered in ``registered_servers``,
or against the Durable Tools Gateway's ``RegistryService.call_tool`` (which starts a plain
workflow wrapping one activity) for everything else. So every byte of real I/O goes through a
replay-safe Temporal primitive, and there is no live session/connection of any kind to manage.

We extend ``agents.mcp.MCPServer`` directly rather than its ``_MCPServerWithClientSession``
subclass (the same choice ``_mcp.py``'s ``_StatelessMCPServerReference`` already makes) —
``_MCPServerWithClientSession`` exists to drive a *real* out-of-process MCP server (stdio, SSE,
Streamable HTTP) over an actual ``mcp.ClientSession``, and ``WorkflowTransport`` is not one:
it's an in-process router that already returns proper ``mcp.types`` objects directly, so
wrapping it in a fake MCP-protocol round trip (JSON-RPC encode -> anyio memory stream -> a
background "router" task decodes and dispatches -> encode a JSON-RPC response -> decode again)
bought nothing but a persistent, stateful session that had to be connected, disconnected, and
raced against concurrent callers. An earlier version did exactly that, to reuse
``_MCPServerWithClientSession``'s tool-list caching/filtering/retry machinery for free — but
every one of those features either doesn't apply here (retrying a *session-level* transient
error makes no sense for a transport with no real connection to drop) or was already disabled
(``cache_tools_list=False``) or unused (``tool_filter``) by every caller in this codebase, so
the session it required bought no benefit against real, confirmed cost: holding that session
open while ``Runner.run_streamed()``'s own streaming machinery was also active deadlocked
(TMPRL1101) in real usage, and even after fixing that by disconnecting once idle, concurrent
tool calls in the same turn (which the OpenAI Agents SDK dispatches via ``asyncio.gather``
whenever a model response contains more than one ``function_call``) raced that shared
session's connect/disconnect lifecycle — confirmed live in ``examples/nexus_hello`` and in a
headless regression test, and only partially, unreliably fixed by reference-counting. Calling
``WorkflowTransport``'s own handlers directly — plain, independent async method calls with no
shared session, router task, or anyio task group of any kind — has no lifecycle to race:
concurrent ``list_tools()``/``call_tool()`` calls are just concurrent coroutines, exactly as
safe here as calling ``execute_operation()`` directly would be.

Still requires ``@workflow.defn(sandboxed=False)``, even though nothing here builds an anyio
task group or cancel scope anymore. Confirmed empirically, not just by absence of anyio calls:
removing the explicit ``_assert_unsandboxed_workflow()`` guard and running this from a default
(sandboxed) workflow doesn't raise a clean error OR work — it hangs. ``agents.mcp.server`` (the
module ``MCPServer`` and ``_DurableMCPServerMarker``'s sibling classes live in) still does
``import anyio`` at module scope regardless of which class you use from it, and re-executing
that inside the sandbox evidently reaches a stuck state rather than a clean
``RestrictedWorkflowAccessError`` -- worse than the error this guard produces, not better, so
the guard stays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping

from agents.mcp import MCPServer

from temporalio import workflow
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.ai_sdks.openai_agents._mcp import _DurableMCPServerMarker

if TYPE_CHECKING:
    from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult, Tool as MCPTool

_INSTALL_MESSAGE = (
    "Nexus-transport MCP support requires the optional `nexus-mcp` extra, which is only "
    "resolvable from an editable checkout of this repo (it path-depends on "
    "nexus/mcp) and requires Python >=3.13. "
    "Install it with `uv sync --extra nexus-mcp`."
)


def _assert_unsandboxed_workflow() -> None:
    """Fail fast, with a clear message, if the calling workflow is sandboxed.

    Without this check, the same misconfiguration hangs instead of raising a clean error --
    confirmed empirically (see module docstring) -- rather than a
    ``RestrictedWorkflowAccessError`` raised deep inside ``agents.mcp.server``'s own
    ``import anyio`` re-executed inside the sandbox.
    """
    if not workflow.in_workflow():
        return
    defn = workflow._Definition.from_class(type(workflow.instance()))
    if defn is not None and defn.sandboxed:
        raise ApplicationError(
            "nexus_transport_mcp_server imports agents.mcp.server, which imports anyio at "
            "module scope -- re-executing that inside Temporal's default sandboxed workflow "
            "runner hangs rather than raising a clean error. Declare the calling workflow "
            "with @workflow.defn(sandboxed=False) — the same annotation nexus_mcp's own "
            "ToolCallWorkflow/ToolRegistryWorkflow use.",
            type="SandboxedWorkflowNotSupported",
            non_retryable=True,
        )


class _NexusTransportMCPServer(_DurableMCPServerMarker, MCPServer):  # type: ignore[misc]
    """MCP server backed directly by ``nexus_mcp``'s ``WorkflowTransport`` — see module
    docstring for why this calls straight into it instead of driving a real MCP session."""

    def __init__(
        self,
        registered_servers: Mapping[str, str],
        name: str | None = None,
        *,
        enabled_services: Callable[[], frozenset[str]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        _assert_unsandboxed_workflow()
        try:
            # imports_passed_through mirrors durable_tools_gateway/__init__.py's own wrapping
            # of its Temporal-decorated imports — cheap and idempotent even if not strictly
            # needed here, so we apply it defensively around the whole (transitive) import.
            with workflow.unsafe.imports_passed_through():
                from transport.workflow_transport import WorkflowTransport
        except ModuleNotFoundError as exc:
            raise RuntimeError(_INSTALL_MESSAGE) from exc

        self._name = name or "nexus-transport"
        self._transport = WorkflowTransport(registered_servers, enabled_services)

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name

    async def connect(self) -> None:
        """Nothing to connect — every call below goes straight to ``WorkflowTransport``."""

    async def cleanup(self) -> None:
        """Nothing to clean up — see ``connect()``."""

    async def __aenter__(self) -> "_NexusTransportMCPServer":
        # MCPServer (unlike _MCPServerWithClientSession, which used to provide this) doesn't
        # define __aenter__/__aexit__ itself -- nexus_transport_mcp_server()'s own
        # `async with nexus_transport_mcp_server(...) as mcp_server:` usage pattern needs it
        # directly on this class.
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        await self.cleanup()

    async def list_tools(
        self, run_context: Any = None, agent: Any = None
    ) -> "list[MCPTool]":
        result = await self._transport._handle_list_tools()
        return result.tools

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any] | None, meta: dict[str, Any] | None = None
    ) -> "CallToolResult":
        from mcp import types

        params_kwargs: dict[str, Any] = {"name": tool_name, "arguments": arguments}
        if meta is not None:
            params_kwargs["_meta"] = types.RequestParams.Meta.model_validate(meta)
        return await self._transport._handle_call_tool(
            types.CallToolRequestParams(**params_kwargs)
        )

    async def list_prompts(self) -> "ListPromptsResult":
        """Nexus-native servers and the Durable Tools Gateway only ever expose tools."""
        from mcp import types

        return types.ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> "GetPromptResult":
        raise NotImplementedError(
            f"MCP server {self.name!r} (Nexus transport) does not support prompts."
        )
