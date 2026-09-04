"""Bridge MCP SDK requests to Nexus tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp import types
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import Context, Extension, MCPServer
from mcp.shared.exceptions import MCPError

from nexus_mcp.resolver import NexusToolResolver, RequestContext, UnknownToolError

# This follows the MCP guidance (https://modelcontextprotocol.io/specification/draft/basic#_meta)
# to use reverse DNS notation.
IDEMPOTENCY_KEY_META_KEY = "io.temporal/idempotencyKey"


def request_context_from_mcp(context: Context[Any, Any]) -> RequestContext:
    """Convert the SDK request context to the resolver context."""
    request = context.request_context
    client_params = request.session.client_params
    client_capabilities = request.session.client_capabilities
    metadata = dict(request.meta or {})
    idempotency_key = metadata.get(IDEMPOTENCY_KEY_META_KEY)
    return RequestContext(
        request_id=request.request_id,
        protocol_version=request.protocol_version,
        client_info=(
            client_params.client_info.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            if client_params is not None
            else {}
        ),
        client_capabilities=(
            client_capabilities.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            if client_capabilities is not None
            else {}
        ),
        metadata=metadata,
        idempotency_key=idempotency_key if isinstance(idempotency_key, str) else None,
    )


class NexusMCPBridge(MCPServer[Any]):
    """Bridge MCP SDK requests to Nexus-backed tools."""

    def __init__(
        self,
        resolver: NexusToolResolver,
        *,
        name: str = "nexus-mcp",
        version: str = "0.1.0",
        instructions: str | None = None,
        extensions: Sequence[Extension] = (),
        enable_tasks: bool = True,
        task_ttl_ms: int | None = 86_400_000,
        task_poll_interval_ms: int = 1_000,
        **options: Any,
    ) -> None:
        self._resolver = resolver
        configured_extensions = list(extensions)
        if enable_tasks and resolver.supports_tasks:
            from .tasks import NexusTasksExtension

            configured_extensions.append(
                NexusTasksExtension(
                    resolver,
                    ttl_ms=task_ttl_ms,
                    poll_interval_ms=task_poll_interval_ms,
                )
            )
        cache_hints = options.pop(
            "cache_hints",
            {"tools/list": CacheHint(ttl_ms=0, scope="private")},
        )
        super().__init__(
            name=name,
            version=version,
            instructions=instructions,
            extensions=configured_extensions,
            cache_hints=cache_hints,
            **options,
        )

    async def list_tools(self) -> list[types.Tool]:
        """List the tools that the configured Nexus services expose."""
        return await self._resolver.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> types.CallToolResult:
        """Call one exact Nexus-backed tool."""
        request_context = (
            request_context_from_mcp(context)
            if context is not None
            else RequestContext()
        )
        try:
            return await self._resolver.call_tool(
                name,
                arguments,
                context=request_context,
            )
        except UnknownToolError as exc:
            raise MCPError(
                code=types.INVALID_PARAMS,
                message=str(exc),
            ) from exc
