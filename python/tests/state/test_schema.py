# ABOUTME: The definition-time field-type checker — which annotations a HarnessState may
# declare, and that every rejection names the offending field and the fix.

"""The definition-time field-type checker."""

from __future__ import annotations

import collections
import dataclasses
import datetime
import decimal
import enum
import uuid
from typing import Any, Deque, Literal, Optional, Union

from typing_extensions import TypedDict
from collections.abc import Iterable, Mapping, MutableSequence, Sequence

import pytest
from pydantic import BaseModel, PrivateAttr, computed_field
from pydantic.errors import PydanticSchemaGenerationError

from temporal_agent_harness.harness.state import HarnessState, StateSchemaError


class _Member(HarnessState):
    """A state model, so `set[_Member]` is a set the checker once accepted."""

    n: int = 0


def _rejects(name: str, annotation: Any) -> StateSchemaError:
    with pytest.raises(StateSchemaError) as info:
        type(
            "Bad",
            (HarnessState,),
            {"__annotations__": {name: annotation}},
        )
    return info.value


@pytest.mark.parametrize(
    "annotation",
    [
        Sequence[int],
        MutableSequence[int],
        Mapping[str, int],
        Iterable[int],
        Deque[int],
        collections.deque,
        collections.OrderedDict[str, int],
        collections.Counter[str],
    ],
)
def test_abstract_and_exotic_containers_are_rejected(annotation):
    error = _rejects("field", annotation)
    assert "field" in str(error)
    assert "Use " in str(error)  # the message names the fix


def test_plain_basemodel_is_rejected():
    class Plain(BaseModel):
        x: int = 0

    assert "HarnessState" in str(_rejects("field", Plain))


def test_dataclass_is_rejected():
    @dataclasses.dataclass
    class Point:
        x: int = 0

    assert "dataclass" in str(_rejects("field", Point))


def test_typeddict_is_rejected():
    class Row(TypedDict):
        a: int

    assert "field" in str(_rejects("field", Row))


def test_bare_containers_are_rejected():
    for annotation in (list, dict, tuple):
        assert "element type" in str(_rejects("field", annotation)) or "parameterized" in str(
            _rejects("field", annotation)
        )


def test_arbitrary_class_is_rejected():
    class Thing:
        pass

    # `arbitrary_types_allowed=False` means pydantic refuses the schema first;
    # either way the failure is at class-definition time, not at runtime.
    with pytest.raises((StateSchemaError, PydanticSchemaGenerationError)):
        type("Bad", (HarnessState,), {"__annotations__": {"field": Thing}})


def test_nesting_is_checked_recursively():
    class Plain(BaseModel):
        x: int = 0

    assert "field" in str(_rejects("field", list[Plain]))
    assert "field" in str(_rejects("field", dict[str, Sequence[int]]))
    assert "field" in str(_rejects("field", tuple[list[Plain], ...]))
    assert "field" in str(_rejects("field", Optional[Sequence[int]]))


def test_private_attributes_are_rejected():
    with pytest.raises(StateSchemaError, match="private attributes"):

        class Bad(HarnessState):
            x: int = 0
            _cache: dict = PrivateAttr(default_factory=dict)


def test_computed_fields_are_rejected():
    with pytest.raises(StateSchemaError, match="computed fields"):

        class Bad(HarnessState):
            x: int = 0

            @computed_field  # type: ignore[misc]
            @property
            def double(self) -> int:
                return self.x * 2


def test_any_warns_but_is_allowed():
    with pytest.warns(UserWarning, match="escape hatch"):

        class Escape(HarnessState):
            payload: Any = None

    value = Escape(payload={"mutable": []})
    assert value.payload == {"mutable": []}


class Color(enum.Enum):
    RED = "red"


class Accepted(HarnessState):
    """Every annotation the design says is allowed."""

    s: str = ""
    i: int = 0
    f: float = 0.0
    b: bool = False
    raw: bytes = b""
    dec: decimal.Decimal = decimal.Decimal(0)
    when: datetime.datetime = datetime.datetime(2020, 1, 1)
    day: datetime.date = datetime.date(2020, 1, 1)
    clock: datetime.time = datetime.time(1, 2)
    span: datetime.timedelta = datetime.timedelta(seconds=1)
    ident: uuid.UUID = uuid.UUID(int=0)
    color: Color = Color.RED
    tag: Literal["a", "b"] = "a"
    maybe: int | None = None
    either: Union[int, str] = 0


def test_allowed_scalars_are_accepted():
    value = Accepted()
    assert value.model_dump(mode="json")["tag"] == "a"


def test_nested_harness_state_in_every_container():
    class Node(HarnessState):
        n: int = 0

    class Holder(HarnessState):
        as_list: list[Node] = []
        as_dict: dict[str, Node] = {}
        as_nested: dict[str, list[Node]] = {}
        as_tuple: tuple[Node, ...] = ()
        as_optional: Node | None = None

    holder = Holder(as_list=[Node(n=1)], as_tuple=(Node(n=2),))
    assert holder.as_list[0].n == 1
    assert holder.as_tuple[0].n == 2


def test_self_referencing_model_is_accepted():
    class Tree(HarnessState):
        name: str = ""
        children: list["Tree"] = []

    Tree.model_rebuild()
    tree = Tree(name="root", children=[Tree(name="leaf")])
    assert tree.children[0].name == "leaf"


def test_tuple_forms():
    class Shapes(HarnessState):
        fixed: tuple[int, str] = (0, "")
        variadic: tuple[int, ...] = ()
        empty: tuple[()] = ()

    shapes = Shapes(fixed=(1, "a"), variadic=(1, 2, 3))
    assert shapes.fixed == (1, "a")
    assert shapes.variadic == (1, 2, 3)


@pytest.mark.parametrize(
    "annotation",
    [set, frozenset, set[str], frozenset[str], set[int | str], set[_Member]],
    ids=["bare-set", "bare-frozenset", "set-str", "frozenset-str", "set-union", "set-model"],
)
def test_sets_are_rejected_whatever_their_shape(annotation):
    """No set may be state, and the message has to say why rather than just refuse.

    JSON has no set and RFC 6902 has no set operation, so a set could only ever be published by
    re-sending the whole collection — the coarsest op in a layer whose point is that a change
    costs what it touched. Keeping that array stable across workers meant sorting on every read,
    which only works when the elements are orderable: `set[str | None]` fell through to CPython's
    hash order, so two workers replaying one history published different bytes.

    Parameterizing it is not the fix, so a bare `set` must NOT be told to add an element type —
    it gets the same answer as `set[str]`.
    """
    error = _rejects("tags", annotation)
    assert "RFC 6902" in str(error)
    assert "list[...]" in str(error)
    assert "element type" not in str(error)
