"""Render a Playpen trace in the text format ``dogwood replay`` reads.

One event per line::

    @<seconds> scope(principal: P, resource: R) [request_context(input: {...})]
        NS::Action::"<tool>"::<kind>(input: {...}[, output: {...}], callerPrincipal: P,
        callerResource: R, requestId: "<tool id>")

Values are written as Cedar literals: strings, integers (Cedar ``Long``), booleans, records and
sets. There is no Cedar null, so a ``None`` field is left out of its record — which is what an
optional attribute is in Cedar. Floats have no faithful Cedar form and are rejected: carry money
as integer cents.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .models import DogwoodEvent

# Every Playpen call goes through the one Code Mode gateway, so the resource is fixed.
RESOURCE_ID = "code-mode"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_LONG_MIN, _LONG_MAX = -(2**63), 2**63 - 1


def render_trace(namespace: str, principal: str, events: Iterable[DogwoodEvent]) -> str:
    """The whole trace, one line per event, in order."""
    return "".join(render_event(namespace, principal, e) + "\n" for e in events)


def render_event(namespace: str, principal: str, event: DogwoodEvent) -> str:
    principal_ref = f"{namespace}::User::{_string(principal)}"
    resource_ref = f"{namespace}::Gateway::{_string(RESOURCE_ID)}"
    input_record = _record(event.input)
    fields = [f"input: {input_record}"]
    if event.output is not None:
        fields.append(f"output: {_record(event.output)}")
    fields += [
        f"callerPrincipal: {principal_ref}",
        f"callerResource: {resource_ref}",
        f"requestId: {_string(event.request_id)}",
    ]
    parts = [f"@{event.timestamp}", f"scope(principal: {principal_ref}, resource: {resource_ref})"]
    if event.kind == "request":
        # The decision point: Dogwood builds the Cedar request's context from this.
        parts.append(f"request_context(input: {input_record})")
    parts.append(
        f"{namespace}::Action::{_string(event.action)}::{event.kind}({', '.join(fields)})"
    )
    return " ".join(parts)


def literal(value: Any) -> str:
    """``value`` as a Cedar literal. Raises ``TypeError`` for a value with no Cedar form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not _LONG_MIN <= value <= _LONG_MAX:
            raise TypeError(f"{value} does not fit a Cedar Long")
        return str(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, dict):
        return _record(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(literal(v) for v in value) + "]"
    if isinstance(value, float):
        raise TypeError(
            f"{value!r} is a float, which has no faithful Cedar form; carry it as an integer "
            "(money as cents)"
        )
    raise TypeError(f"{type(value).__name__} value {value!r} has no Cedar literal form")


def _record(fields: dict[str, Any]) -> str:
    rendered = []
    for key, value in fields.items():
        if not isinstance(key, str) or not _IDENTIFIER.match(key):
            raise TypeError(f"record key {key!r} is not a Cedar identifier")
        if value is None:
            continue
        rendered.append(f"{key}: {literal(value)}")
    return "{ " + ", ".join(rendered) + " }" if rendered else "{ }"


def _string(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif " " <= ch <= "~":
            out.append(ch)
        else:
            out.append(f"\\u{{{ord(ch):x}}}")
    out.append('"')
    return "".join(out)
