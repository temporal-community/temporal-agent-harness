"""Public-facing helpers to author a Nexus service that implements MCP contracts.

MCPOverNexusServiceHandler: a base class that implements a `list_tools` operation based on introspecting
                            the service's other operations. This allows authors to only need to implement
                            their business logic.

@nexus_mcp_tool: a decorator that allows authors to write a plain async method with typed
                 parameters and a docstring, and have it automatically wired as a Nexus operation
                 with a Pydantic input model derived from the method's own signature, and discoverable
                 via `list_tools`.
                 This allows authors to annotate only a subset of their service as MCP tools, and also
                 allow them to use a single decorator that handles the MCP in/out data modeling for them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar, cast, get_type_hints

import nexusrpc
import nexusrpc.handler
from nexusrpc.handler import StartOperationContext
from pydantic import BaseModel

from .internal_utils import (
    _NEXUS_MCP_TOOL_MARKER,
    CALL_TOOL_OPERATION,
    LIST_TOOLS_OPERATION,
    _pydantic_model_from_signature,
    build_tool_dicts,
)

__all__ = [
    "CALL_TOOL_OPERATION",
    "LIST_TOOLS_OPERATION",
    "ListToolsOutput",
    "MCPOverNexusServiceHandler",
    "nexus_mcp_tool",
]


class ListToolsOutput(BaseModel):
    """All tools one MCP-over-Nexus service exposes."""

    tools: list[dict[str, Any]] = []
    """Serialised mcp.types.Tool dicts, names already prefixed with the service name."""



class MCPOverNexusServiceHandler:
    """Base handler class giving concrete MCP-over-Nexus handlers a default `list_tools()`
    implementation that derives the list of tools from methods that are decorated with `@nexus_mcp_tool`.
    """

    #: Forwarded to build_tool_dicts' inherently_safe. Override on your subclass if your
    #: tools should be tagged readOnlyHint=True (letting approval policies auto-approve them).
    _mcp_over_nexus_inherently_safe: bool = False

    @nexusrpc.handler.sync_operation
    async def list_tools(self, ctx: StartOperationContext, input: None) -> ListToolsOutput:
        """Return this service's own tools, derived from its other Nexus operations."""
        tools = build_tool_dicts(
            type(self), inherently_safe=self._mcp_over_nexus_inherently_safe
        )
        return ListToolsOutput(tools=tools)


_NexusMcpToolFunc = TypeVar("_NexusMcpToolFunc", bound=Callable[..., Any])


def nexus_mcp_tool(fn: _NexusMcpToolFunc) -> _NexusMcpToolFunc:
    """A decorator to turn a plain typed async method into a fully-wired Nexus operation. When
    combined with MCPOverNexusServiceHandler, this allows authors to write a Nexus service
    that implements MCP contracts, without the author needing to hand-write a lot of the
    boilerplate code. Example usage:

    import nexusrpc.handler
    from authoring import MCPOverNexusServiceHandler, nexus_mcp_tool

    @nexusrpc.handler.service_handler(name="weather-tools")
    class WeatherToolServer(MCPOverNexusServiceHandler):
    
        # Here lies a tool that gets the weather forecast.
        @nexus_mcp_tool
        async def get_forecast(self, city: str, days: int = 3) -> str:
            ...

    In the Temporal worker file, pass this `WeatherToolServer()` instance like so:
    `Worker(nexus_service_handlers=[WeatherToolServer(), ...])`
    """
    input_model = _pydantic_model_from_signature(fn)
    return_type = get_type_hints(fn).get("return", Any)

    async def operation_method(self: Any, ctx: StartOperationContext, input: Any) -> Any:
        return await fn(self, **input.model_dump())

    operation_method.__name__ = fn.__name__
    operation_method.__doc__ = fn.__doc__
    operation_method.__annotations__ = {
        "ctx": StartOperationContext,
        "input": input_model,
        "return": return_type,
    }

    # TODO: Handle async nexus operations as well.
    operation = nexusrpc.handler.sync_operation(operation_method)
    # Stamped on sync_operation's OWN return value -- see _NEXUS_MCP_TOOL_MARKER's comment
    # for why -- so build_tool_dicts can tell this apart from a hand-declared operation.
    setattr(operation, _NEXUS_MCP_TOOL_MARKER, True)
    return cast("_NexusMcpToolFunc", operation)
