"""Generate the sandbox's static type-check stubs (and the model-facing interface) from tools.

Code Mode gives a script a set of host functions — one per harness tool — and type-checks the
script against their signatures BEFORE running it, so a wrong argument type or an unknown result
key is reported to the author instead of failing mid-run. That requires Python stub source
describing each host function's parameters and result shape.

This module derives that source by reflecting over each tool's model-facing signature and,
recursively, the fields of any pydantic model it references (rendered as ``TypedDict``s so the
checker can validate key access on results). Two renderings are produced from the same walk:

  * :func:`render_type_check_stubs` — the stub source fed to the sandbox's type checker.
  * :func:`render_host_interface` — the same signatures + ``TypedDict``s, but with each tool's
    docstring attached, for embedding in the run-code tool's own docstring (what the model reads).

Type checking is only as good as the stubs, so fidelity is enforced, not approximated: a type
that cannot be rendered faithfully raises :class:`CodeModeStubError` (naming the tool and the
offending parameter/field) rather than degrading to ``Any``. Result shapes reflect what a script
actually observes — a tool's return value rendered with ``model_dump(mode="json")`` — so
``datetime`` / ``UUID`` / ``Decimal`` / ``bytes`` become ``str``, enums become a ``Literal`` of
their values, and sets/tuples become lists.

The generated stub begins with ``from __future__ import annotations``, so every ``TypedDict``
field and function annotation is a lazy string: the checker resolves names regardless of
definition order, and self-referential or mutually-recursive models need no special handling.
"""

from __future__ import annotations

import collections.abc as cabc
import enum
import inspect
import sys
import typing
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

_NoneType = type(None)

# Rendered as-is (Python builtins the sandbox understands directly).
_PRIMITIVES: dict[type, str] = {str: "str", int: "int", float: "float", bool: "bool"}

# Types whose ``model_dump(mode="json")`` form is a plain string, so a script sees a ``str``.
_JSON_STRING_TYPES: frozenset[type] = frozenset(
    {datetime, date, time, UUID, Decimal, Path, bytes}
)


class CodeModeStubError(Exception):
    """Raised when a tool's signature cannot be rendered into faithful type-check stubs.

    Carries the tool and the parameter/field path so the developer can fix the tool's types.
    Raised at ``code_mode_tool`` construction time (i.e. at ``@workflow.init``), so an
    unrepresentable tool set fails fast rather than silently under-validating scripts."""


def _unwrap_annotated(tp: Any) -> Any:
    """Strip ``Annotated[X, ...]`` metadata down to ``X`` (repeatedly, for nested wrapping)."""
    while hasattr(tp, "__metadata__"):
        tp = tp.__args__[0]
    return tp


def resolve_hints(obj: Callable[..., Any]) -> dict[str, Any]:
    """Resolved type hints for ``obj`` (parameters + ``return``), as concrete type objects.

    Tools defined in a module with ``from __future__ import annotations`` carry their annotations
    as strings; this resolves them against the tool's own module globals so the generator sees
    real types. Returns ``{}`` if resolution fails wholesale (e.g. an unresolvable name) — callers
    then fall back to the raw signature annotation and report a clear error if it is still a
    string."""
    try:
        module = sys.modules.get(getattr(obj, "__module__", "") or "")
        globalns = getattr(module, "__dict__", None)
        return typing.get_type_hints(obj, globalns=globalns)
    except Exception:
        return {}


class _StubBuilder:
    """Accumulates the ``TypedDict`` definitions referenced by a set of tools, and renders each
    tool's signature. One builder handles one render pass over one tool list."""

    def __init__(self) -> None:
        self._name_by_model: dict[type[BaseModel], str] = {}
        self._model_order: list[type[BaseModel]] = []
        self._fields_by_model: dict[type[BaseModel], list[str]] = {}
        self._used_names: set[str] = set()
        self._uses_any = False
        self._uses_literal = False

    def build(
        self, tools: list[Callable[..., Awaitable[Any]]], *, with_doc: bool
    ) -> str:
        """Render ``tools`` to source: the referenced ``TypedDict``s followed by one ``async def``
        per tool. ``with_doc`` attaches each tool's docstring as the stub body (for the
        model-facing interface); otherwise the body is ``...`` (for the type checker)."""
        func_blocks = [self._render_tool(t, with_doc=with_doc) for t in tools]

        typing_imports: list[str] = []
        if self._model_order:
            typing_imports.append("TypedDict")
        if self._uses_any:
            typing_imports.append("Any")
        if self._uses_literal:
            typing_imports.append("Literal")

        header = ["from __future__ import annotations"]
        if typing_imports:
            header.append(f"from typing import {', '.join(sorted(typing_imports))}")

        typed_dicts: list[str] = []
        for model in self._model_order:
            name = self._name_by_model[model]
            fields = self._fields_by_model[model]
            body = "\n".join(fields) if fields else "    pass"
            typed_dicts.append(f"class {name}(TypedDict):\n{body}")

        sections = ["\n".join(header)]
        if typed_dicts:
            sections.append("\n\n".join(typed_dicts))
        if func_blocks:
            sections.append("\n\n".join(func_blocks))
        return "\n\n\n".join(sections) + "\n"

    def _render_tool(
        self, tool: Callable[..., Awaitable[Any]], *, with_doc: bool
    ) -> str:
        sig = inspect.signature(tool)
        name = tool.__name__
        hints = resolve_hints(tool)
        rendered_params: list[str] = []
        emitted_star = False
        for pname, param in sig.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise CodeModeStubError(
                    f"tool {name!r} has a *args/**kwargs parameter {pname!r}; Code Mode needs "
                    f"an explicit, fully typed signature to type-check scripts against it."
                )
            if param.kind == param.KEYWORD_ONLY and not emitted_star:
                rendered_params.append("*")
                emitted_star = True
            if pname in hints:
                annotation = hints[pname]
            elif param.annotation is not inspect.Parameter.empty:
                annotation = param.annotation
            else:
                raise CodeModeStubError(
                    f"tool {name!r} parameter {pname!r} has no type annotation; Code Mode "
                    f"requires typed parameters for static validation."
                )
            rendered = self._render_type(annotation, f"{name}({pname})")
            rendered_params.append(f"{pname}: {rendered}")

        if "return" in hints:
            return_annotation = hints["return"]
        elif sig.return_annotation is not inspect.Signature.empty:
            return_annotation = sig.return_annotation
        else:
            raise CodeModeStubError(
                f"tool {name!r} has no return annotation; Code Mode requires a typed return "
                f"so scripts can be checked against the result shape."
            )
        ret = self._render_type(return_annotation, f"{name} return")

        signature = f"async def {name}({', '.join(rendered_params)}) -> {ret}"
        doc = inspect.cleandoc(tool.__doc__) if with_doc and tool.__doc__ else None
        if doc is None:
            return f"{signature}: ..."
        indented = "\n".join(f"    {line}".rstrip() for line in doc.splitlines())
        return f'{signature}:\n    """\n{indented}\n    """'

    def _render_type(self, tp: Any, ctx: str) -> str:
        tp = _unwrap_annotated(tp)

        if isinstance(tp, str):
            raise CodeModeStubError(
                f"{ctx}: annotation is the string {tp!r} (a stringized/forward reference). "
                f"Code Mode needs concrete type objects; avoid `from __future__ import "
                f"annotations` on tool signatures."
            )
        if tp is Any:
            self._uses_any = True
            return "Any"
        if tp is None or tp is _NoneType:
            return "None"
        if tp in _PRIMITIVES:
            return _PRIMITIVES[tp]
        if tp in _JSON_STRING_TYPES:
            return "str"
        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            return self._render_enum(tp, ctx)
        if isinstance(tp, type) and issubclass(tp, BaseModel):
            return self._register_model(tp)

        if tp in (list, set, frozenset, tuple, dict):
            raise CodeModeStubError(
                f"{ctx}: bare `{tp.__name__}` without an element type; annotate the element type "
                f"(e.g. list[str], dict[str, int]) for static validation."
            )

        origin = get_origin(tp)
        args = get_args(tp)

        if origin is Literal:
            self._uses_literal = True
            return f"Literal[{', '.join(repr(a) for a in args)}]"
        if origin in (list, set, frozenset) or origin in (cabc.Sequence, cabc.Set):
            if not args:
                raise CodeModeStubError(
                    f"{ctx}: bare `{getattr(tp, '__name__', tp)}` without an element type; "
                    f"annotate the element type (e.g. list[str]) for static validation."
                )
            return f"list[{self._render_type(args[0], ctx)}]"
        if origin in (dict,) or origin is cabc.Mapping:
            if len(args) != 2:
                raise CodeModeStubError(
                    f"{ctx}: bare `dict`/`Mapping` without key/value types; annotate them "
                    f"(e.g. dict[str, int]) for static validation."
                )
            return (
                f"dict[{self._render_type(args[0], ctx)}, "
                f"{self._render_type(args[1], ctx)}]"
            )
        if origin is tuple:
            return self._render_tuple(args, ctx)
        if origin is Union or origin is getattr(__import__("types"), "UnionType", None):
            return " | ".join(self._render_type(a, ctx) for a in args)

        raise CodeModeStubError(
            f"{ctx}: cannot represent type {tp!r} in Code Mode type-check stubs. Supported: "
            f"primitives, pydantic models, list/set/dict/tuple, unions/optionals, Literal, "
            f"enums, and datetime/UUID/Decimal/bytes (rendered as str). Use one of these, or "
            f"an explicit `Any`, in the tool's signature."
        )

    def _render_tuple(self, args: tuple[Any, ...], ctx: str) -> str:
        # A tuple serializes to a JSON array, so the script sees a list. Variable-length
        # tuple[X, ...] and homogeneous fixed tuples render as list[X]; a heterogeneous fixed
        # tuple has no faithful list element type, so it is rejected rather than widened.
        if not args:
            raise CodeModeStubError(
                f"{ctx}: bare `tuple` without element types; annotate them for static validation."
            )
        if len(args) == 2 and args[1] is Ellipsis:
            return f"list[{self._render_type(args[0], ctx)}]"
        element_types = {a for a in args if a is not Ellipsis}
        if len(element_types) == 1:
            return f"list[{self._render_type(next(iter(element_types)), ctx)}]"
        raise CodeModeStubError(
            f"{ctx}: heterogeneous fixed-length tuple {args!r} has no single JSON element type; "
            f"use a list of one element type or a pydantic model."
        )

    def _render_enum(self, tp: type[enum.Enum], ctx: str) -> str:
        # An enum serializes to its member VALUE, so the script sees that scalar. Render a
        # Literal of the values when they are primitives; otherwise it can't be checked cleanly.
        values = [member.value for member in tp]
        if values and all(isinstance(v, (str, int, bool)) for v in values):
            self._uses_literal = True
            return f"Literal[{', '.join(repr(v) for v in values)}]"
        raise CodeModeStubError(
            f"{ctx}: enum {tp.__name__} has non-primitive member values {values!r}; Code Mode "
            f"can only render enums whose values are str/int/bool."
        )

    def _register_model(self, model: type[BaseModel]) -> str:
        if model in self._name_by_model:
            return self._name_by_model[model]

        name = self._unique_name(model)
        # Reserve the name BEFORE recursing into fields so self/mutual references resolve to it.
        self._name_by_model[model] = name
        self._used_names.add(name)
        self._model_order.append(model)

        try:
            hints = typing.get_type_hints(model)
        except Exception as exc:  # unresolvable forward ref in the model's own annotations
            raise CodeModeStubError(
                f"cannot resolve type hints for model {model.__name__!r}: {exc}"
            ) from exc

        field_lines: list[str] = []
        for field_name, field_info in model.model_fields.items():
            annotation = hints.get(field_name, field_info.annotation)
            rendered = self._render_type(annotation, f"{model.__name__}.{field_name}")
            field_lines.append(f"    {field_name}: {rendered}")
        self._fields_by_model[model] = field_lines
        return name

    def _unique_name(self, model: type[BaseModel]) -> str:
        base = model.__name__
        if base not in self._used_names:
            return base
        # Two distinct classes share a name: disambiguate the later one by its module.
        candidate = f"{base}_{model.__module__.replace('.', '_')}"
        if candidate not in self._used_names:
            return candidate
        counter = 2
        while f"{candidate}_{counter}" in self._used_names:
            counter += 1
        return f"{candidate}_{counter}"


def render_type_check_stubs(tools: list[Callable[..., Awaitable[Any]]]) -> str:
    """Render the Python stub source the sandbox type-checks a Code Mode script against.

    One ``async def <tool_name>(...) -> ...: ...`` per tool, preceded by a ``TypedDict`` for every
    pydantic model referenced by any tool's parameters or result. Raises :class:`CodeModeStubError`
    if any parameter/field/return type cannot be rendered faithfully (see the module docstring)."""
    return _StubBuilder().build(tools, with_doc=False)


def render_host_interface(tools: list[Callable[..., Awaitable[Any]]]) -> str:
    """Render the model-facing host-function interface: the same signatures + ``TypedDict``s as
    :func:`render_type_check_stubs`, but with each tool's docstring attached as the stub body, for
    embedding in the run-code tool's docstring so the model sees what each host function does."""
    return _StubBuilder().build(tools, with_doc=True)
