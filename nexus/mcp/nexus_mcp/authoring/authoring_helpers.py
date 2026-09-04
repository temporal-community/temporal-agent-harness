"""Public helpers for authoring Nexus services that expose MCP tools.

Use ``MCPOverNexusServiceHandler`` as the base class for a Nexus tool service.
It adds the ``list_tools`` operation and builds the tool manifest from marked
operations.

Use ``@nexus_mcp_tool`` on a typed ``def`` or ``async def`` method. It creates
the Pydantic input model and the synchronous Nexus operation.

Use ``@nexus_mcp_operation`` on an existing Nexus operation. The Nexus
decorator continues to control its input, output, name, and execution behavior.

Use ``MCPToolConfig`` to set MCP names, descriptions, annotations, icons, and
metadata on a service or tool.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar, cast, get_type_hints, overload

import mcp.types
import nexusrpc
import nexusrpc.handler
from nexusrpc.handler import StartOperationContext
from pydantic import BaseModel, Field

from .internal_utils import (
    _NEXUS_MCP_TOOL_MARKER,
    LIST_TOOLS_OPERATION,
    _pydantic_model_from_signature,
    build_tool_dicts,
    build_tool_routes,
)

__all__ = [
    "LIST_TOOLS_OPERATION",
    "ListToolsOutput",
    "MCPOverNexusServiceHandler",
    "MCPToolConfig",
    "nexus_mcp_operation",
    "nexus_mcp_tool",
]


@dataclass(frozen=True)
class MCPToolConfig:
    """Configure the MCP definition for one Nexus tool."""

    name: str | None = None
    title: str | None = None
    description: str | None = None
    annotations: mcp.types.ToolAnnotations | None = None
    icons: Sequence[mcp.types.Icon] | None = None
    meta: Mapping[str, Any] | None = None


class ListToolsOutput(BaseModel):
    """Contain the tools that one MCP-over-Nexus service exposes."""

    tools: list[dict[str, Any]] = Field(default_factory=list)
    routes: dict[str, str] = Field(default_factory=dict)


class MCPOverNexusServiceHandler:
    """Add a generated ``list_tools`` operation to a Nexus service handler."""

    mcp_tool_defaults = MCPToolConfig()

    @nexusrpc.handler.sync_operation
    async def list_tools(
        self, ctx: StartOperationContext, input: None
    ) -> ListToolsOutput:
        """Return the tools that this service exposes."""
        handler_type = type(self)
        return ListToolsOutput(
            tools=build_tool_dicts(handler_type),
            routes=build_tool_routes(handler_type),
        )


_NexusMcpToolFunc = TypeVar("_NexusMcpToolFunc", bound=Callable[..., Any])


def _config(
    *,
    name: str | None,
    title: str | None,
    description: str | None,
    annotations: mcp.types.ToolAnnotations | None,
    icons: Sequence[mcp.types.Icon] | None,
    meta: Mapping[str, Any] | None,
) -> MCPToolConfig:
    return MCPToolConfig(
        name=name,
        title=title,
        description=description,
        annotations=annotations,
        icons=icons,
        meta=meta,
    )


@overload
def nexus_mcp_tool(fn: _NexusMcpToolFunc, /) -> _NexusMcpToolFunc: ...


@overload
def nexus_mcp_tool(
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: mcp.types.ToolAnnotations | None = None,
    icons: Sequence[mcp.types.Icon] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> Callable[[_NexusMcpToolFunc], _NexusMcpToolFunc]: ...


def nexus_mcp_tool(
    fn: _NexusMcpToolFunc | None = None,
    /,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    annotations: mcp.types.ToolAnnotations | None = None,
    icons: Sequence[mcp.types.Icon] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> _NexusMcpToolFunc | Callable[[_NexusMcpToolFunc], _NexusMcpToolFunc]:
    """Convert a typed Python method into a synchronous Nexus MCP operation.

    The method can use ``def`` or ``async def``. Its parameters become a Pydantic
    input model. Use :func:`nexus_mcp_operation` for an existing Nexus operation.
    """
    tool_config = _config(
        name=name,
        title=title,
        description=description,
        annotations=annotations,
        icons=icons,
        meta=meta,
    )

    def decorate(method: _NexusMcpToolFunc) -> _NexusMcpToolFunc:
        input_model = _pydantic_model_from_signature(method)
        return_type = get_type_hints(method).get("return", Any)

        @wraps(method)
        async def operation_method(
            self: Any, ctx: StartOperationContext, input: BaseModel
        ) -> Any:
            arguments = {
                field_name: getattr(input, field_name)
                for field_name in type(input).model_fields
            }
            result = method(self, **arguments)
            if inspect.isawaitable(result):
                return await result
            return result

        operation_method.__annotations__ = {
            "ctx": StartOperationContext,
            "input": input_model,
            "return": return_type,
        }

        operation_decorator = nexusrpc.handler.sync_operation(name=tool_config.name)
        operation = operation_decorator(cast(Any, operation_method))
        setattr(operation, _NEXUS_MCP_TOOL_MARKER, tool_config)
        return cast("_NexusMcpToolFunc", operation)

    if fn is None:
        return decorate
    return decorate(fn)


@overload
def nexus_mcp_operation(fn: _NexusMcpToolFunc, /) -> _NexusMcpToolFunc: ...


@overload
def nexus_mcp_operation(
    *,
    title: str | None = None,
    description: str | None = None,
    annotations: mcp.types.ToolAnnotations | None = None,
    icons: Sequence[mcp.types.Icon] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> Callable[[_NexusMcpToolFunc], _NexusMcpToolFunc]: ...


def nexus_mcp_operation(
    fn: _NexusMcpToolFunc | None = None,
    /,
    *,
    title: str | None = None,
    description: str | None = None,
    annotations: mcp.types.ToolAnnotations | None = None,
    icons: Sequence[mcp.types.Icon] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> _NexusMcpToolFunc | Callable[[_NexusMcpToolFunc], _NexusMcpToolFunc]:
    """Expose an existing Nexus operation as an MCP tool.

    Place this decorator above the Nexus operation decorator. The Nexus decorator
    controls the operation name, input type, output type, and execution behavior.
    """
    tool_config = _config(
        name=None,
        title=title,
        description=description,
        annotations=annotations,
        icons=icons,
        meta=meta,
    )

    def decorate(operation: _NexusMcpToolFunc) -> _NexusMcpToolFunc:
        if nexusrpc.get_operation(operation) is None:
            raise TypeError(
                "nexus_mcp_operation must be above a Nexus operation decorator"
            )
        setattr(operation, _NEXUS_MCP_TOOL_MARKER, tool_config)
        return operation

    if fn is None:
        return decorate
    return decorate(fn)
