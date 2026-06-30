"""Registration utilities for Nexus service workers.

Workers call these at startup to derive MCP tool metadata (name, description,
JSON schema) directly from a Nexus service handler class, then register those
tools with the gateway via RegistryService.register_nexus.
"""

from __future__ import annotations

import re
from typing import Any

import mcp.types
import nexusrpc
import pydantic

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9-]{1,64}$")


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
) -> list[dict[str, Any]]:
    """Build serialised ``mcp.types.Tool`` dicts from a Nexus service handler class.

    Extracts operation names, docstrings, and Pydantic input schemas directly
    from the handler class — no intermediate registry object needed.

    Args:
        handler_class:  A ``@service_handler``-decorated class.
        inherently_safe: If ``True``, tools are tagged ``readOnlyHint=True``
                         so approval policies can auto-approve them.

    Returns:
        A list of dicts, each a ``mcp.types.Tool.model_dump()`` with the tool
        name already prefixed as ``{service_name}_{op_name}``.
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
