"""Internal helpers for Nexus MCP authoring."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, get_type_hints

import mcp.types
import nexusrpc
import pydantic
from pydantic import BaseModel, TypeAdapter

if TYPE_CHECKING:
    from .authoring_helpers import MCPToolConfig

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
LIST_TOOLS_OPERATION = "list_tools"
_NEXUS_MCP_TOOL_MARKER = "_nexus_mcp_tool"


def validate_service_name(name: str) -> None:
    """Check that a service name can be part of an MCP tool name."""
    if not _NAME_RE.match(name):
        raise ValueError(f"Service name {name!r} must match [a-zA-Z0-9_-]{{1,64}}")


def _annotations_dict(value: mcp.types.ToolAnnotations | None) -> dict[str, Any]:
    if value is None:
        return {}
    return value.model_dump(exclude_none=True)


def _merge_config(defaults: MCPToolConfig, tool: MCPToolConfig) -> MCPToolConfig:
    from .authoring_helpers import MCPToolConfig

    annotation_values = {
        **_annotations_dict(defaults.annotations),
        **_annotations_dict(tool.annotations),
    }
    annotations = (
        mcp.types.ToolAnnotations.model_validate(annotation_values)
        if annotation_values
        else None
    )
    metadata = {**dict(defaults.meta or {}), **dict(tool.meta or {})}
    return MCPToolConfig(
        name=tool.name,
        title=tool.title if tool.title is not None else defaults.title,
        description=(
            tool.description if tool.description is not None else defaults.description
        ),
        annotations=annotations,
        icons=tool.icons if tool.icons is not None else defaults.icons,
        meta=metadata or None,
    )


def _object_schema(annotation: Any) -> dict[str, Any] | None:
    if annotation in (None, Any):
        return None
    try:
        schema = TypeAdapter(annotation).json_schema()
    except (pydantic.PydanticUserError, TypeError, ValueError):
        return None
    return schema if schema.get("type") == "object" else None


def _build_tool_entries(
    handler_class: type,
    *,
    exclude_operations: frozenset[str] = frozenset({LIST_TOOLS_OPERATION}),
) -> list[tuple[dict[str, Any], str]]:
    """Build MCP definitions and their Nexus operation names."""
    from .authoring_helpers import MCPToolConfig

    defn = nexusrpc.get_service_definition(handler_class)
    if defn is None:
        raise ValueError(f"{handler_class.__name__} is not a Nexus service handler")

    validate_service_name(defn.name)
    defaults = getattr(handler_class, "mcp_tool_defaults", MCPToolConfig())
    if not isinstance(defaults, MCPToolConfig):
        raise TypeError("mcp_tool_defaults must be an MCPToolConfig")
    if defaults.name is not None:
        raise ValueError("mcp_tool_defaults cannot set a tool name")

    entries: list[tuple[dict[str, Any], str]] = []
    for op in defn.operation_definitions.values():
        if op.name in exclude_operations:
            continue
        func = getattr(handler_class, op.method_name or op.name, None)
        marker = getattr(func, _NEXUS_MCP_TOOL_MARKER, None)
        if not isinstance(marker, MCPToolConfig):
            continue
        config = _merge_config(defaults, marker)

        name = f"{defn.name}_{op.name}"
        if not _NAME_RE.match(name):
            raise ValueError(f"Generated tool name {name!r} is not LLM-compatible")

        input_schema = _object_schema(op.input_type) or {}
        output_schema = _object_schema(op.output_type)
        description = config.description
        if description is None and func.__doc__:
            description = inspect.cleandoc(func.__doc__)

        tool = mcp.types.Tool(
            name=name,
            title=config.title,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            icons=list(config.icons) if config.icons is not None else None,
            annotations=config.annotations,
            _meta=dict(config.meta) if config.meta is not None else None,
        )
        entries.append(
            (
                tool.model_dump(mode="json", by_alias=True, exclude_none=True),
                op.name,
            )
        )
    return entries


def build_tool_dicts(
    handler_class: type,
    *,
    exclude_operations: frozenset[str] = frozenset({LIST_TOOLS_OPERATION}),
) -> list[dict[str, Any]]:
    """Build MCP tool definitions from marked operations on a Nexus service."""
    return [
        tool
        for tool, _ in _build_tool_entries(
            handler_class,
            exclude_operations=exclude_operations,
        )
    ]


def build_tool_routes(
    handler_class: type,
    *,
    exclude_operations: frozenset[str] = frozenset({LIST_TOOLS_OPERATION}),
) -> dict[str, str]:
    """Map each public MCP tool name to its Nexus operation name."""
    return {
        tool["name"]: operation
        for tool, operation in _build_tool_entries(
            handler_class,
            exclude_operations=exclude_operations,
        )
    }


def _pydantic_model_from_signature(fn: Callable[..., Any]) -> type[BaseModel]:
    """Create a Pydantic input model from method parameters after ``self``."""
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    fields: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            raise TypeError(
                "nexus_mcp_tool does not support *args or **kwargs "
                f"(found {name!r} on {fn.__qualname__})"
            )
        annotation = hints.get(name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)
    return pydantic.create_model(f"{fn.__name__}_input", **fields)  # type: ignore[call-overload]
