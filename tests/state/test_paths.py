# ABOUTME: RFC 6901 pointer construction — escaping of field names and dict keys, the JSON
# rendering of non-string keys, and key annotations whose JSON rendering is not injective,
# so a patch path always resolves to the same node a consumer sees in the snapshot.

"""RFC 6901 pointer construction: escaping, non-string dict keys, key renderings."""

from __future__ import annotations

import datetime
import decimal
import enum
import uuid
from typing import Literal

import pytest
from pydantic import TypeAdapter

from .support import StateHost
from temporal_agent_harness.harness.state import HarnessState
from temporal_agent_harness.harness.state.errors import StateSchemaError
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


class Colour(enum.StrEnum):
    RED = "red"


class Rank(enum.IntEnum):
    ONE = 1


class Keyed(HarnessState):
    by_str: dict[str, int] = {}
    by_int: dict[int, str] = {}
    by_date: dict[datetime.date, str] = {}
    by_bool: dict[bool, str] = {}
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


def test_bool_dict_keys_use_their_json_rendering():
    # JSON spells a bool object key `true`/`false`; Python's `str(True)` does not.
    events: list = []
    ref = StateHost(events.append).state("k", Keyed())
    with ref.mutate() as d:
        d.by_bool[True] = "yes"
        d.by_bool[False] = "no"
    assert _ops(ref, events) == [
        {"op": "add", "path": "/by_bool/true", "value": "yes"},
        {"op": "add", "path": "/by_bool/false", "value": "no"},
    ]
    doc = apply_ops(events[0].value, events[-1].ops)
    assert doc == ref.current.model_dump(mode="json")


@pytest.mark.parametrize(
    "key,sample",
    [
        (str, "a"),
        (int, 7),
        (float, 1.5),
        (bool, True),
        (bool, False),
        (bytes, b"hi"),
        (decimal.Decimal, decimal.Decimal("1.50")),
        (datetime.date, datetime.date(2020, 1, 2)),
        (datetime.datetime, datetime.datetime(2020, 1, 2, 3, 4, 5)),
        (datetime.time, datetime.time(1, 2, 3)),
        (datetime.timedelta, datetime.timedelta(seconds=90)),
        (uuid.UUID, uuid.UUID(int=1)),
        (Colour, Colour.RED),
        (Rank, Rank.ONE),
    ],
    ids=lambda v: repr(v),
)
def test_a_pointer_segment_is_the_key_pydantic_would_serialize(key, sample):
    """The segment must be whatever `model_dump` puts in the snapshot - for every key type.

    `DraftDict._seg` renders the key itself rather than dumping a whole dict per op, so
    it can drift from the key serializer pydantic actually uses. It did: bools reached
    the snapshot as `true`/`false` and the pointer said `True`, which pointed every op
    on a bool-keyed dict at a key that does not exist. This is the pin that catches the
    next such divergence in whichever type introduces it.
    """
    from temporal_agent_harness.harness.state.base import AdapterNode
    from temporal_agent_harness.harness.state.containers import DraftDict

    pydantic_key = next(iter(TypeAdapter(dict[key, str]).dump_python({sample: "v"}, mode="json")))
    drafted = DraftDict.__new__(DraftDict)
    drafted._key = AdapterNode(key)
    assert drafted._seg(sample) == escape(pydantic_key)


@pytest.mark.parametrize(
    "key",
    [int | str, datetime.date | str, float | int],
    ids=["int-or-str", "date-or-str", "float-or-int"],
)
def test_union_dict_keys_are_rejected(key):
    """A union key has no injective JSON rendering, so no pointer can be faithful.

    ``1`` and ``"1"`` are two distinct Python keys but one JSON object key, so the
    snapshot itself can only hold one of them - there is nothing a pointer could
    say that would keep a consumer in sync.  The only place to catch it is here.
    """
    with pytest.raises(StateSchemaError):
        type(
            "Ambiguous",
            (HarnessState,),
            {"__annotations__": {"m": dict[key, str]}, "m": {}},
        )


def test_a_literal_key_whose_members_collide_as_json_is_rejected():
    """`Literal[1, "1"]` is two Python keys and one JSON object key - the union bug again.

    It needs no union to get there, so the union check alone does not catch it: the
    snapshot held `{"1": "str-one"}` while Python held both, and both ops pointed at
    `/m/1`.
    """
    with pytest.raises(StateSchemaError):
        type(
            "Colliding",
            (HarnessState,),
            {"__annotations__": {"m": dict[Literal[1, "1"], str]}, "m": {}},
        )


def test_a_literal_key_whose_members_stay_distinct_is_fine():
    """The rule is about collisions, not about Literal keys."""

    class Sized(HarnessState):
        m: dict[Literal["small", "large"], int] = {}

    events: list = []
    ref = StateHost(events.append).state("s", Sized())
    with ref.mutate() as d:
        d.m["small"] = 1
    assert _ops(ref, events) == [{"op": "add", "path": "/m/small", "value": 1}]


def test_model_dict_keys_are_rejected():
    """A model cannot be a JSON object key, and the two spellings did not even agree.

    Before this was refused the snapshot rendered the key as the model's repr
    (`"id='a'"`) and the pointer as its dumped dict (`{'id': 'a'}`), so every op on such
    a dict was addressed at a key no consumer has.
    """

    class Todo(HarnessState):
        id: str = ""

    with pytest.raises(StateSchemaError):
        type(
            "ByModel",
            (HarnessState,),
            {"__annotations__": {"m": dict[Todo, str]}, "m": {}},
        )


def test_container_dict_keys_are_rejected():
    with pytest.raises(StateSchemaError):
        type(
            "ByList",
            (HarnessState,),
            {"__annotations__": {"m": dict[tuple[int, int], str]}, "m": {}},
        )
