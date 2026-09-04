"""Internal implementation details backing `authoring_helpers`, not intended to be exported.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any, get_type_hints

import mcp.types
import nexusrpc
import pydantic
from pydantic import BaseModel

# Basic tool/function name constraint: ^[a-zA-Z0-9_-]{1,64}$
# (only alphanums, underscore, and dash; 1–64 chars).
# Inspired by OpenAI's tool name constraints -- maybe similar tool name restrictions in
# other agents SDK.
# TODO(long-nt-tran): Consider making this more restrictive/permissive depending on
#                     what the LLM agents SDKs generally expect.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# The operation name every MCP-over-Nexus service reserves for tool discovery per MCP protocol.
LIST_TOOLS_OPERATION = "list_tools"

# Stamped by nexus_mcp_tool (authoring_helpers.py) onto the callable it returns, so
# build_tool_dicts can tell a nexus_mcp_tool-authored operation apart from one declared by
# hand with a bare @nexusrpc.handler.sync_operation -- only the former should be
# auto-listed as an LLM tool.
_NEXUS_MCP_TOOL_MARKER = "_nexus_mcp_tool"


def validate_service_name(name: str) -> None:
    """Check a service name is valid.

    Raises:
        ValueError: if `name` doesn't match `_NAME_RE`.
    """
    if not _NAME_RE.match(name):
        raise ValueError(f"Service name {name!r} must match [a-zA-Z0-9_-]{{1,64}}")


def build_tool_dicts(
    handler_class: type,
    *,
    inherently_safe: bool = False,
    exclude_operations: frozenset[str] = frozenset({LIST_TOOLS_OPERATION}),
) -> list[dict[str, Any]]:
    """Build serialised `mcp.types.Tool` dicts from a Nexus service handler class.

    Extracts operation names, docstrings, and Pydantic input schemas directly from the
    handler class.
    
    Only operations authored via `nexus_mcp_tool` are included. Other Nexus operation declared
    without `@nexus_mcp_tool` are not listed as an LLM tool.

    Args:
        handler_class: A `@service_handler`-decorated class.
        inherently_safe: If `True`, tools are tagged `readOnlyHint=True` so approval
            policies can auto-approve them.
        exclude_operations: Operation names to omit from the result, on top of the
            nexus_mcp_tool-only filter above. Defaults to just `list_tools`; pass an empty
            `frozenset` to include it too.

    Returns:
        A list of dicts, each a `mcp.types.Tool.model_dump()` with the tool name already
        prefixed as `{service_name}_{op_name}`.
    """
    defn = nexusrpc.get_service_definition(handler_class)
    if defn is None:
        raise ValueError(f"{handler_class.__name__} is not a Nexus service handler")

    validate_service_name(defn.name)

    tools: list[dict[str, Any]] = []
    for op in defn.operation_definitions.values():
        if op.name in exclude_operations:
            continue
        attr_name = op.method_name or op.name
        func = getattr(handler_class, attr_name, None)
        if func is None or not callable(func):
            continue
        if not getattr(func, _NEXUS_MCP_TOOL_MARKER, False):
            continue

        name = f"{defn.name}_{op.name}"
        if not _NAME_RE.match(name):
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


def _pydantic_model_from_signature(fn: Callable[..., Any]) -> type[BaseModel]:
    """Synthesize a Pydantic model from a function's own parameter list (skipping `self`)."""
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
