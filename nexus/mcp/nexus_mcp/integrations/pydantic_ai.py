"""Public Pydantic AI adapters for Nexus tools.

Use ``NexusToolset`` outside a Temporal workflow. Pass it a connected Temporal
``Client``, and add it to ``Agent.toolsets``.

Use ``ResolverBackedToolset`` when you create the ``NexusToolResolver``.

Use ``RequestContextFactory`` as the callback type for creating Nexus request
metadata and headers.

Example::

    from pydantic_ai import Agent

    nexus_tools = NexusToolset.for_service(
        temporal_client,
        "weather",
        "weather-endpoint",
    )
    agent = Agent("openai:gpt-5.1", toolsets=[nexus_tools])
"""

from __future__ import annotations

import base64
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from mcp import types as mcp_types
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import BinaryContent, BinaryImage
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from nexus_mcp.execution import StandaloneNexusExecutor
from nexus_mcp.resolver import NexusToolResolver, RequestContext, UnknownToolError

if TYPE_CHECKING:
    from temporalio.client import Client

RequestContextFactory = Callable[
    [RunContext[Any], str | None, dict[str, Any] | None],
    RequestContext | Awaitable[RequestContext],
]

_TOOL_ARGUMENTS_VALIDATOR = SchemaValidator(
    schema=core_schema.dict_schema(
        core_schema.str_schema(),
        core_schema.any_schema(),
    )
)


def _tool_metadata(tool: mcp_types.Tool) -> dict[str, Any]:
    return {
        "meta": tool.meta,
        "annotations": (
            tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
            if tool.annotations
            else None
        ),
    }


def _map_content(part: mcp_types.ContentBlock) -> Any:
    if isinstance(part, mcp_types.TextContent):
        if part.text.startswith(("[", "{")):
            try:
                return json.loads(part.text)
            except ValueError:
                pass
        return part.text
    if isinstance(part, mcp_types.ImageContent):
        return BinaryImage(data=base64.b64decode(part.data), media_type=part.mime_type)
    if isinstance(part, mcp_types.AudioContent):
        return BinaryContent(
            data=base64.b64decode(part.data), media_type=part.mime_type
        )
    if isinstance(part, mcp_types.ResourceLink):
        return str(part.uri)
    if isinstance(part, mcp_types.EmbeddedResource):
        resource = part.resource
        if isinstance(resource, mcp_types.TextResourceContents):
            return resource.text
        return BinaryContent(
            data=base64.b64decode(resource.blob),
            media_type=resource.mime_type or "application/octet-stream",
        )
    raise TypeError(f"Unsupported MCP content type: {type(part).__name__}")


def _error_message(result: mcp_types.CallToolResult) -> str:
    messages = [
        part.text for part in result.content if isinstance(part, mcp_types.TextContent)
    ]
    return "\n".join(messages) or "The Nexus tool call failed."


def _map_result(result: mcp_types.CallToolResult) -> Any:
    if result.is_error:
        raise ModelRetry(_error_message(result))
    if result.structured_content is not None:
        return result.structured_content
    content = [_map_content(part) for part in result.content]
    if len(content) == 1:
        return content[0]
    return content


class ResolverBackedToolset(AbstractToolset[Any]):
    """Adapt a Nexus tool resolver to Pydantic AI."""

    def __init__(
        self,
        resolver: NexusToolResolver,
        *,
        id: str | None = None,
        max_retries: int | None = None,
        include_return_schema: bool | None = None,
        request_context_factory: RequestContextFactory | None = None,
    ) -> None:
        self._resolver = resolver
        self._id = id or resolver.name
        self._max_retries = max_retries
        self._include_return_schema = include_return_schema
        self._request_context_factory = request_context_factory

    @property
    def id(self) -> str:
        return self._id

    @property
    def resolver(self) -> NexusToolResolver:
        return self._resolver

    async def get_tools(
        self,
        ctx: RunContext[Any],
    ) -> dict[str, ToolsetTool[Any]]:
        max_retries = (
            self._max_retries if self._max_retries is not None else ctx.max_retries
        )
        request_context = await self._make_request_context(ctx, None, None)
        tools = await self._resolver.list_tools(request_context)
        return {
            tool.name: ToolsetTool(
                toolset=self,
                tool_def=ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    parameters_json_schema=tool.input_schema,
                    metadata=_tool_metadata(tool),
                    return_schema=tool.output_schema,
                    include_return_schema=self._include_return_schema,
                ),
                max_retries=max_retries,
                args_validator=_TOOL_ARGUMENTS_VALIDATOR,
            )
            for tool in tools
        }

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        request_context = await self._make_request_context(ctx, name, tool_args)
        try:
            result = await self._resolver.call_tool(
                name,
                tool_args,
                context=request_context,
            )
        except UnknownToolError as error:
            raise ModelRetry(str(error)) from error
        return _map_result(result)

    async def _make_request_context(
        self,
        ctx: RunContext[Any],
        name: str | None,
        tool_args: dict[str, Any] | None,
    ) -> RequestContext:
        if self._request_context_factory is None:
            return RequestContext()
        request_context = self._request_context_factory(ctx, name, tool_args)
        if inspect.isawaitable(request_context):
            return await request_context
        return request_context


class NexusToolset(ResolverBackedToolset):
    """Expose Nexus tools to Pydantic AI outside a Temporal workflow."""

    def __init__(
        self,
        client: Client,
        registered_servers: Mapping[str, str],
        *,
        name: str = "nexus-tools",
        allowed_servers: frozenset[str] | None = None,
        id: str | None = None,
        max_retries: int | None = None,
        include_return_schema: bool | None = None,
        request_context_factory: RequestContextFactory | None = None,
    ) -> None:
        super().__init__(
            NexusToolResolver(
                registered_servers,
                StandaloneNexusExecutor(client),
                name=name,
                allowed_servers=allowed_servers,
            ),
            id=id,
            max_retries=max_retries,
            include_return_schema=include_return_schema,
            request_context_factory=request_context_factory,
        )

    @classmethod
    def for_service(
        cls,
        client: Client,
        service: str,
        endpoint: str,
        **kwargs: Any,
    ) -> NexusToolset:
        """Create a toolset for one Nexus service."""
        kwargs.setdefault("name", service)
        return cls(client, {service: endpoint}, **kwargs)


__all__ = ["NexusToolset", "RequestContextFactory", "ResolverBackedToolset"]
