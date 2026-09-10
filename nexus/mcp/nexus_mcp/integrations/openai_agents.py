"""Public OpenAI Agents adapters for Nexus tools.

Use ``NexusMCPServer`` outside a Temporal workflow. Pass it a connected
Temporal ``Client``, and add it to ``Agent.mcp_servers``.

Use ``WorkflowNexusMCPServer`` inside a Temporal workflow. Add it to
``Agent.mcp_servers``.

Use ``RequestContextFactory`` as the callback type for creating Nexus request
metadata and headers.

Example::

    from agents import Agent

    nexus_tools = NexusMCPServer.for_service(
        temporal_client,
        "weather",
        "weather-endpoint",
    )
    agent = Agent(name="weather-agent", mcp_servers=[nexus_tools])
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from agents.mcp import MCPServer
from mcp import types

from nexus_mcp.execution import StandaloneNexusExecutor, WorkflowNexusExecutor
from nexus_mcp.resolver import (
    IDEMPOTENCY_KEY_META_KEY,
    NexusToolResolver,
    RequestContext,
    UnknownToolError,
)

if TYPE_CHECKING:
    from temporalio.client import Client

RequestContextFactory = Callable[
    [dict[str, Any] | None],
    RequestContext | Awaitable[RequestContext],
]


def _error_result(error: Exception) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=str(error))],
        is_error=True,
    )


def _request_context(meta: dict[str, Any] | None) -> RequestContext:
    metadata = dict(meta or {})
    idempotency_key = metadata.get(IDEMPOTENCY_KEY_META_KEY)
    return RequestContext(
        metadata=metadata,
        idempotency_key=(idempotency_key if isinstance(idempotency_key, str) else None),
    )


class _ResolverBackedMCPServer(MCPServer):  # type: ignore[misc]
    """Adapt a Nexus tool resolver to the OpenAI Agents SDK."""

    def __init__(
        self,
        resolver: NexusToolResolver,
        *,
        request_context_factory: RequestContextFactory | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._resolver = resolver
        self._request_context_factory = request_context_factory or _request_context

    @property
    def name(self) -> str:
        return self._resolver.name

    @property
    def resolver(self) -> NexusToolResolver:
        return self._resolver

    async def connect(self) -> None:
        """Do nothing because the adapter does not own a connection."""

    async def cleanup(self) -> None:
        """Do nothing because the adapter does not own a connection."""

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.cleanup()

    async def list_tools(
        self,
        run_context: Any = None,
        agent: Any = None,
    ) -> list[types.Tool]:
        return await self._resolver.list_tools(await self._make_request_context(None))

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        try:
            return await self._resolver.call_tool(
                tool_name,
                arguments,
                context=await self._make_request_context(meta),
            )
        except UnknownToolError as error:
            return _error_result(error)

    async def list_prompts(self) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[])

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> types.GetPromptResult:
        raise NotImplementedError(f"MCP server {self.name!r} does not support prompts.")

    async def _make_request_context(
        self,
        meta: dict[str, Any] | None,
    ) -> RequestContext:
        context = self._request_context_factory(meta)
        if inspect.isawaitable(context):
            return await context
        return context


class NexusMCPServer(_ResolverBackedMCPServer):
    """Expose Nexus tools to an OpenAI agent outside a Temporal workflow."""

    def __init__(
        self,
        client: Client,
        registered_servers: Mapping[str, str],
        *,
        name: str = "nexus-tools",
        allowed_servers: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            NexusToolResolver(
                registered_servers,
                StandaloneNexusExecutor(client),
                name=name,
                allowed_servers=allowed_servers,
            ),
            **kwargs,
        )

    @classmethod
    def for_service(
        cls,
        client: Client,
        service: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Self:
        """Create an adapter for one Nexus service."""
        kwargs.setdefault("name", service)
        return cls(client, {service: endpoint}, **kwargs)


class WorkflowNexusMCPServer(_ResolverBackedMCPServer):
    """Expose Nexus tools to an OpenAI agent in a Temporal workflow."""

    def __init__(
        self,
        registered_servers: Mapping[str, str],
        *,
        name: str = "nexus-tools",
        allowed_servers: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            NexusToolResolver(
                registered_servers,
                WorkflowNexusExecutor(),
                name=name,
                allowed_servers=allowed_servers,
            ),
            **kwargs,
        )

    @classmethod
    def for_service(
        cls,
        service: str,
        endpoint: str,
        **kwargs: Any,
    ) -> Self:
        """Create an adapter for one Nexus service."""
        kwargs.setdefault("name", service)
        return cls({service: endpoint}, **kwargs)


__all__ = [
    "NexusMCPServer",
    "RequestContextFactory",
    "WorkflowNexusMCPServer",
]
