"""Model-facing sandbox tools: any OpenAI capability's own tools, as harness tools.

The capabilities run in the workflow, bound to the agent's :class:`CapabilitySession`, where
each sandbox operation (exec, read, write, PTY) is an activity, the same way the OpenAI Agents
Temporal plugin runs them. So any capability works, built-in or a developer's own, and no
tool needs an activity of its own. Each tool becomes an inline harness tool that any model
SDK (and Code Mode) can use, with the name, description and parameters the capability gives
it.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from typing import Annotated, Any, Literal, Union

from agents.sandbox import Manifest
from agents.sandbox.capabilities import Capability
from agents.tool import CustomTool, FunctionTool, ToolOutputImage, ToolOutputText
from agents.tool_context import ToolContext
from pydantic import Field, create_model
from pydantic_core import to_json
from temporalio import workflow
from temporalio.exceptions import ApplicationError, TemporalError

from temporal_agent_harness.harness.agent_workflow import _CURRENT_TOOL_ID, tool_defn
from temporal_agent_harness.harness.sandbox_image import SandboxImage

_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "null": type(None),
}

# A custom (freeform) tool takes one raw string rather than JSON arguments; as a harness
# tool, that string is its one parameter.
_CUSTOM_INPUT = "input"
_CUSTOM_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        _CUSTOM_INPUT: {
            "type": "string",
            "description": "The tool's input, written as its description says.",
        }
    },
    "required": [_CUSTOM_INPUT],
}


def tools_for(
    capabilities: Sequence[Capability], ensure_running: Callable[[], Awaitable[object]]
) -> list[Callable[..., Awaitable[Any]]]:
    """The harness tools for bound ``capabilities``. Each call runs ``ensure_running``
    first, so the session's state (its manifest, for path checks) is there."""
    return [as_harness_tool(t, ensure_running) for c in capabilities for t in c.tools()]


def as_harness_tool(
    tool: Any, ensure_running: Callable[[], Awaitable[object]]
) -> Callable[..., Awaitable[Any]]:
    """An inline harness tool that runs an OpenAI ``FunctionTool`` or ``CustomTool``.

    Its parameters are built from the tool's JSON schema. An exception other than a Temporal
    one comes back to the caller as a non-retryable ``ApplicationError``, rather than
    failing the workflow task. An image result becomes a :class:`SandboxImage`.
    """
    if isinstance(tool, FunctionTool):
        schema: Mapping[str, Any] = tool.params_json_schema
    elif isinstance(tool, CustomTool):
        schema = _CUSTOM_INPUT_SCHEMA
    else:
        raise TypeError(
            f"capability tool {getattr(tool, 'name', tool)!r} is a {type(tool).__name__}; "
            "only FunctionTool and CustomTool tools can run as harness tools"
        )
    defs: Mapping[str, Any] = schema.get("$defs", {})
    required = set(schema.get("required", ()))
    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    for name, prop in schema.get("properties", {}).items():
        annotation, default = _field(name, prop, name in required, defs)
        annotation = Annotated[annotation, Field(description=prop.get("description"))]
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.POSITIONAL_OR_KEYWORD, default=default, annotation=annotation
            )
        )
        annotations[name] = annotation
    # Parameters without a default come first, as Python requires.
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    signature = inspect.Signature(params, return_annotation=Any)

    async def body(*args: Any, **kwargs: Any) -> Any:
        # Only what the caller passed: the tool applies its own defaults.
        arguments = signature.bind(*args, **kwargs).arguments
        raw = arguments[_CUSTOM_INPUT] if isinstance(tool, CustomTool) else to_json(arguments).decode()
        await ensure_running()
        context = ToolContext(
            context=None,
            tool_name=tool.name,
            tool_call_id=_CURRENT_TOOL_ID.get() or str(workflow.uuid4()),
            tool_arguments=raw,
        )
        try:
            result = await tool.on_invoke_tool(context, raw)
        except TemporalError:
            raise
        except Exception as e:
            raise ApplicationError(str(e), type=type(e).__name__, non_retryable=True) from e
        return _model_facing(result)

    body.__name__ = body.__qualname__ = tool.name
    body.__doc__ = tool.description
    body.__signature__ = signature  # type: ignore[attr-defined]
    body.__annotations__ = {**annotations, "return": Any}
    return tool_defn()(body)


def instructions_for(capabilities: Sequence[Capability], manifest: Manifest | None) -> str:
    """The capabilities' own prompt fragments, joined.

    ``Capability.instructions`` is async but, for most capabilities, never awaits anything,
    so it is driven to completion here without an event loop.
    """
    resolved = manifest if manifest is not None else Manifest()
    fragments = [_run_without_awaiting(c.instructions(resolved)) for c in capabilities]
    return "\n\n".join(f for f in fragments if f)


def _run_without_awaiting(coro: Coroutine[Any, Any, str | None]) -> str | None:
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise RuntimeError("capability instructions awaited I/O; they must be computed statically")


def _model_facing(result: Any) -> Any:
    if isinstance(result, ToolOutputImage) and result.image_url:
        header, _, data = result.image_url.partition(",")
        return SandboxImage(mime_type=header.removeprefix("data:").split(";")[0], data=data)
    if isinstance(result, ToolOutputText):
        return result.text
    return result


def _field(
    name: str, schema: Mapping[str, Any], required: bool, defs: Mapping[str, Any]
) -> tuple[Any, Any]:
    """The annotation and default for one JSON schema property. An optional property with no
    default may be left out, so it defaults to ``None``."""
    annotation = _annotation(schema, defs, name)
    if required:
        return annotation, inspect.Parameter.empty
    if "default" in schema:
        return annotation, schema["default"]
    return annotation | None, None


def _annotation(schema: Mapping[str, Any], defs: Mapping[str, Any], name: str) -> Any:
    """A Python type for a JSON schema, close enough for each model SDK to rebuild the
    schema from it. The tool validates its own arguments, so constraints are left out."""
    if ref := schema.get("$ref"):
        ref_name = ref.rsplit("/", 1)[-1]
        return _annotation(defs[ref_name], defs, ref_name)
    if "enum" in schema:
        return Literal[tuple(schema["enum"])]
    if "const" in schema:
        return Literal[schema["const"]]
    if variants := schema.get("anyOf") or schema.get("oneOf"):
        return Union[tuple(_annotation(v, defs, name) for v in variants)]
    kind = schema.get("type")
    if isinstance(kind, list):
        return Union[tuple(_annotation({**schema, "type": k}, defs, name) for k in kind)]
    if kind == "array":
        return list[_annotation(schema.get("items", {}), defs, name)]  # type: ignore[misc]
    if kind == "object":
        properties: Mapping[str, Any] = schema.get("properties", {})
        if properties:
            required = set(schema.get("required", ()))
            fields: dict[str, Any] = {}
            for field_name, prop in properties.items():
                annotation, default = _field(field_name, prop, field_name in required, defs)
                fields[field_name] = (
                    annotation,
                    Field(
                        ... if default is inspect.Parameter.empty else default,
                        description=prop.get("description"),
                    ),
                )
            return create_model(schema.get("title") or name.title().replace("_", ""), **fields)
        extra = schema.get("additionalProperties")
        return dict[str, _annotation(extra, defs, name) if isinstance(extra, Mapping) else Any]  # type: ignore[misc]
    return _JSON_TYPES.get(kind, Any) if isinstance(kind, str) else Any
