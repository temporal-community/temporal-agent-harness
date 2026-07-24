"""authoring — the shared contract for authoring an MCP-over-Nexus service.

A Nexus-native MCP server is a tool service that exposes its capabilities as plain Nexus
operations and is reached DIRECTLY by WorkflowTransport — no gateway, no activity in between.
Any such server implements this contract so WorkflowTransport can discover its tools
generically, the same way for every registered server::

    @nexusrpc.service(name="my-service")
    class MyToolService(MCPOverNexusService):
        do_something: nexusrpc.Operation[DoSomethingInput, str]

    @nexusrpc.handler.service_handler(service=MyToolService)
    class MyToolServiceHandler(MCPOverNexusServiceHandler):
        @nexusrpc.handler.sync_operation
        async def do_something(self, ctx, input: DoSomethingInput) -> str:
            \"\"\"Do the thing.\"\"\"
            ...

``MCPOverNexusService`` contributes exactly one operation, ``list_tools``, that every
Nexus-native MCP server must answer so ``WorkflowTransport`` can build its tool menu (fanning
that call out across every server registered against it — see
``temporal_agent_harness.ai_sdks.openai_agents``'s ``NexusMcpServerRegistry``).
``MCPOverNexusServiceHandler`` gives you that operation's implementation for free, derived
from your OWN handler's other operations via :func:`build_tool_dicts` — author your business
operations only; never implement ``list_tools`` yourself.

This is the counterpart to the "Durable Tools Gateway" flow: a Nexus-native MCP server is
called directly (WorkflowTransport -> this server's own Nexus endpoint), whereas a 3rd-party
(non-Nexus) MCP server is registered against a proxy (e.g. the gateway) and called *through*
it — a proxy also implements this same ``list_tools`` contract but derives its answer from
tools registered on its own behalf (via ``register_external``), not from its own Nexus
surface — see ``durable_tools_gateway.registry_service_handler``'s ``RegistryServiceHandler``,
which implements ``list_tools`` directly rather than extending
:class:`MCPOverNexusServiceHandler`. There's nothing to declare at registration time either
way — ``WorkflowTransport`` tells the two apart structurally (see its own module docstring).
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, TypeVar, cast, get_type_hints

import mcp.types
import nexusrpc
import nexusrpc.handler
import pydantic
from nexusrpc.handler import StartOperationContext
from pydantic import BaseModel

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")

# The operation name every MCP-over-Nexus service reserves for tool discovery. Excluded from
# build_tool_dicts' own output by default, so a service's tool list never advertises tool
# discovery itself as a callable tool.
LIST_TOOLS_OPERATION = "list_tools"

# The operation name a PROXY-style MCP-over-Nexus service (one fronting a dynamically
# discovered tool set it can't know ahead of time, e.g. the Durable Tools Gateway) answers a
# tool call through — WorkflowTransport calls this generically, by name, on any registered
# entry that isn't itself the tool's own registered name (see workflow_transport.py's
# structural direct-vs-proxy detection; there is no separate "role" to declare anywhere).
CALL_TOOL_OPERATION = "call_tool"


class ListToolsOutput(BaseModel):
    """All tools one MCP-over-Nexus service exposes."""

    tools: list[dict[str, Any]] = []
    """Serialised mcp.types.Tool dicts, names already prefixed with the service name."""


@nexusrpc.service
class MCPOverNexusService:
    """Base Nexus service every MCP-over-Nexus server extends.

    Contributes ``list_tools``; concrete services add their own business operations
    alongside it (nexusrpc service classes support inheritance — the concrete service's
    definition includes both).
    """

    list_tools: nexusrpc.Operation[None, ListToolsOutput]
    """Return this service's own tools. WorkflowTransport calls this directly (no gateway,
    no activity) against every registered Nexus-native MCP server to build its tool menu."""


def get_nexus_service_name(handler_class: type) -> str:
    """Return the Nexus service name for a ``@service_handler``-decorated class."""
    defn = nexusrpc.get_service_definition(handler_class)
    if defn is None:
        raise ValueError(f"{handler_class.__name__} is not a Nexus service handler")
    return defn.name


def build_tool_dicts(
    handler_class: type,
    *,
    inherently_safe: bool = False,
    exclude_operations: frozenset[str] = frozenset({LIST_TOOLS_OPERATION}),
) -> list[dict[str, Any]]:
    """Build serialised ``mcp.types.Tool`` dicts from a Nexus service handler class.

    Extracts operation names, docstrings, and Pydantic input schemas directly from the
    handler class — no intermediate registry object needed. ``list_tools`` itself is
    excluded by default (it's the discovery mechanism, not a tool).

    Args:
        handler_class: A ``@service_handler``-decorated class.
        inherently_safe: If ``True``, tools are tagged ``readOnlyHint=True`` so approval
            policies can auto-approve them.
        exclude_operations: Operation names to omit from the result. Defaults to just
            ``list_tools``; pass an empty ``frozenset`` to include everything.

    Returns:
        A list of dicts, each a ``mcp.types.Tool.model_dump()`` with the tool name already
        prefixed as ``{service_name}_{op_name}``.
    """
    defn = nexusrpc.get_service_definition(handler_class)
    if defn is None:
        raise ValueError(f"{handler_class.__name__} is not a Nexus service handler")

    if not _SERVICE_NAME_RE.match(defn.name):
        raise ValueError(
            f"Service name {defn.name!r} must match [a-zA-Z0-9-]{{1,64}} "
            "(no underscores — underscore is the service/operation delimiter)"
        )

    tools: list[dict[str, Any]] = []
    for op in defn.operation_definitions.values():
        if op.name in exclude_operations:
            continue
        attr_name = op.method_name or op.name
        func = getattr(handler_class, attr_name, None)
        if func is None or not callable(func):
            continue

        name = f"{defn.name}_{op.name}"
        if not _TOOL_NAME_RE.match(name):
            raise ValueError(f"Generated tool name {name!r} is not LLM-compatible")

        schema: dict[str, Any] = {}
        if op.input_type is not None and issubclass(op.input_type, pydantic.BaseModel):
            schema = op.input_type.model_json_schema()

        annotations = (
            mcp.types.ToolAnnotations(readOnlyHint=True) if inherently_safe else None
        )
        tool = mcp.types.Tool(
            name=name,
            description=func.__doc__.strip() if func.__doc__ else None,
            inputSchema=schema,
            annotations=annotations,
        )
        tools.append(tool.model_dump())

    return tools


class MCPOverNexusServiceHandler:
    """Base handler mixin giving concrete MCP-over-Nexus handlers a default ``list_tools()``
    for free, derived from their own other operations via :func:`build_tool_dicts`.

    Not every MCP-over-Nexus handler needs this — one whose tool list comes from somewhere
    *other* than its own Nexus operations (e.g. the Durable Tools Gateway, which aggregates
    tools registered on behalf of 3rd-party servers) implements ``list_tools`` directly
    instead of extending this mixin. See ``durable_tools_gateway.RegistryServiceHandler``.
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


def _pydantic_model_from_signature(fn: Callable[..., Any]) -> type[BaseModel]:
    """Synthesize a Pydantic model from a function's own parameter list (skipping ``self``).

    Deliberately minimal — no docstring-derived per-parameter descriptions, no support for
    ``*args``/``**kwargs``. This is the only piece :func:`nexus_mcp_tool` needs: a
    JSON-serialisable input type with the right field names, types, and defaults, derived
    from plain type-hinted parameters. (A fuller version of this — with per-parameter
    docstring descriptions, ``RunContextWrapper`` injection, etc. — already exists as
    ``agents.function_schema``, but pulling that in here would make authoring a Nexus-native
    MCP tool depend on the entire OpenAI Agents SDK for a caller who may not be using it at
    all — the same reason this whole package has no framework dependencies.)
    """
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    fields: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError(
                f"nexus_mcp_tool does not support *args/**kwargs parameters "
                f"(found {name!r} on {fn.__qualname__})"
            )
        annotation = hints.get(name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)
    return pydantic.create_model(f"{fn.__name__}_input", **fields)  # type: ignore[call-overload]


_NexusMcpToolFunc = TypeVar("_NexusMcpToolFunc", bound=Callable[..., Any])


def nexus_mcp_tool(fn: _NexusMcpToolFunc) -> _NexusMcpToolFunc:
    """Turn a plain typed async method into a fully-wired Nexus operation — no separate
    Pydantic input model, no ``nexusrpc.Operation[...]`` declaration.

    Mirrors FastMCP's ``@mcp.tool()`` ergonomics: write a plain method with typed parameters
    and a docstring, and get a working ``nexusrpc.handler.sync_operation`` for free. The
    input model is synthesized from the function's own signature (see
    :func:`_pydantic_model_from_signature`).

    Combine with :class:`MCPOverNexusServiceHandler` to also get ``list_tools`` for free,
    derived from the same synthesized operations::

        import nexusrpc.handler
        from authoring import MCPOverNexusServiceHandler, nexus_mcp_tool

        @nexusrpc.handler.service_handler(name="weather-tools")
        class WeatherToolServer(MCPOverNexusServiceHandler):
            @nexus_mcp_tool
            async def get_forecast(self, city: str, days: int = 3) -> str:
                \"\"\"Get the weather forecast for a city.\"\"\"
                ...

    Pass this ``WeatherToolServer()`` instance to ``Worker(nexus_service_handlers=[...])``
    exactly as you would a hand-authored one — nothing else changes.

    .. important::
        Always give the class an explicit ``name=`` in ``@service_handler`` (as above).
        Without a ``service=`` argument, ``@service_handler`` synthesizes the
        ``ServiceDefinition`` from the class's own methods and defaults the service name to
        the *Python class name* — but that name becomes the tool-name prefix shown to the
        LLM (``weather-tools_get_forecast``), and it can't contain underscores (the
        service/operation delimiter — see :func:`build_tool_dicts`). Relying on the
        class-name default couples your tool names to a Python identifier you may want to
        rename later for unrelated reasons.

    This intentionally does NOT support hand-authored Pydantic models with custom
    validators/defaults beyond what plain parameter annotations express, ``*args``/
    ``**kwargs``, or sharing one ``Operation[...]`` type across multiple services — use the
    manual ``Operation[...]`` + ``@nexusrpc.handler.sync_operation`` pattern for those; the
    two are meant to coexist on the same handler class as needed.

    Args:
        fn: An ``async def`` method (``self`` plus typed, JSON-serialisable parameters)
            returning a value the caller's declared return type can represent.
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

    return cast(
        "_NexusMcpToolFunc", nexusrpc.handler.sync_operation(operation_method)
    )


__all__ = [
    "LIST_TOOLS_OPERATION",
    "CALL_TOOL_OPERATION",
    "ListToolsOutput",
    "MCPOverNexusService",
    "MCPOverNexusServiceHandler",
    "build_tool_dicts",
    "get_nexus_service_name",
    "nexus_mcp_tool",
]
