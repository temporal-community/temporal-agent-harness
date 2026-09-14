# ABOUTME: RFC 6901 pointer construction — escaping of field names and dict keys, and the
# JSON rendering of non-string keys, so a patch path resolves to the same node a consumer sees.

"""RFC 6901 pointer construction: escaping and non-string dict keys."""

from __future__ import annotations

import datetime

import pytest

from .support import StateHost
from temporal_agent_harness.harness.state import HarnessState
from temporal_agent_harness.harness.state.session import escape

from .replay import apply_ops


class Weird(HarnessState):
    """A field whose Python name needs escaping in a JSON pointer."""

    plain: int = 0


# `a/b` is not a legal identifier, so the field has to be built dynamically.
Slashed = type(
    "Slashed",
    (HarnessState,),
    {"__annotations__": {"a/b": int, "c~d": str}, "a/b": 0, "c~d": ""},
)


class Keyed(HarnessState):
    by_str: dict[str, int] = {}
    by_int: dict[int, str] = {}
    by_date: dict[datetime.date, str] = {}
    nested: dict[str, list[int]] = {}


def test_escape_helper():
    assert escape("plain") == "plain"
    assert escape("a/b") == "a~1b"
    assert escape("~x") == "~0x"
    assert escape("~/") == "~0~1"
    # order matters: `~` must be escaped before `/`
    assert escape("a~1b") == "a~01b"


def _ops(ref, events):
    return events[-1].ops


def test_field_names_are_escaped():
    events: list = []
    ref = StateHost(events.append).state("s", Slashed())
    with ref.mutate() as d:
        setattr(d, "a/b", 5)
        setattr(d, "c~d", "x")
    assert _ops(ref, events) == [
        {"op": "replace", "path": "/a~1b", "value": 5},
        {"op": "replace", "path": "/c~0d", "value": "x"},
    ]
    doc = apply_ops(events[0].value, events[-1].ops)
    assert doc == ref.current.model_dump(mode="json")


def test_dict_keys_are_escaped():
    events: list = []
    ref = StateHost(events.append).state("k", Keyed())
    with ref.mutate() as d:
        d.by_str["a/b"] = 1
        d.by_str["~x"] = 2
        d.by_str["plain"] = 3
        del d.by_str["a/b"]
    assert _ops(ref, events) == [
        {"op": "add", "path": "/by_str/a~1b", "value": 1},
        {"op": "add", "path": "/by_str/~0x", "value": 2},
        {"op": "add", "path": "/by_str/plain", "value": 3},
        {"op": "remove", "path": "/by_str/a~1b"},
    ]
    doc = apply_ops(events[0].value, events[-1].ops)
    assert doc == ref.current.model_dump(mode="json")


def test_non_string_dict_keys_use_their_json_rendering():
    events: list = []
    ref = StateHost(events.append).state("k", Keyed())
    with ref.mutate() as d:
        d.by_int[7] = "seven"
        d.by_date[datetime.date(2020, 1, 2)] = "epoch"
    assert _ops(ref, events) == [
        {"op": "add", "path": "/by_int/7", "value": "seven"},
        {"op": "add", "path": "/by_date/2020-01-02", "value": "epoch"},
    ]
    doc = apply_ops(events[0].value, events[-1].ops)
    assert doc == ref.current.model_dump(mode="json")


def test_paths_into_a_value_under_an_escaped_key():
    events: list = []
    ref = StateHost(events.append).state("k", Keyed())
    with ref.mutate() as d:
        d.nested["a/b"] = [1]
        d.nested["a/b"].append(2)
        d.nested["a/b"][0] = 9
    assert _ops(ref, events) == [
        {"op": "add", "path": "/nested/a~1b", "value": [1]},
        {"op": "add", "path": "/nested/a~1b/-", "value": 2},
        {"op": "replace", "path": "/nested/a~1b/0", "value": 9},
    ]
    doc = apply_ops(events[0].value, events[-1].ops)
    assert doc == ref.current.model_dump(mode="json")


def test_root_pointer_is_the_empty_string():
    events: list = []
    ref = StateHost(events.append).state("k", Keyed())
    ref.set(Keyed(by_str={"a": 1}))
    assert events[-1].ops[0]["path"] == ""
    doc = apply_ops(events[0].value, events[-1].ops)
    assert doc == ref.current.model_dump(mode="json")
