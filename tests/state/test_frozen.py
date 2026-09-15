# ABOUTME: Immutability at rest — every blocked container method raises FrozenError, nested
# containers inside tuples are frozen too, and a committed value is frozen all the way down.

"""Immutability at rest: every blocked method raises, nested containers are frozen."""

from __future__ import annotations

import random

import pytest

from temporal_agent_harness.harness.state import FrozenError
from temporal_agent_harness.harness.state.containers import FrozenDict, FrozenList

from .support import StateHost
from .models import AgentState, Group, Todo


@pytest.fixture
def value() -> AgentState:
    return AgentState(
        groups=[Group(name="g", todos=[Todo(id="t")], meta={"k": "v"})],
        index={"a": [Todo(id="x")]},
        tags=["one", "two"],
        matrix=([1, 2], [3]),
    )


def test_containers_are_frozen_subclasses(value: AgentState):
    assert type(value.groups) is FrozenList
    assert type(value.groups[0].todos) is FrozenList
    assert type(value.groups[0].meta) is FrozenDict
    assert type(value.index) is FrozenDict
    assert type(value.index["a"]) is FrozenList
    assert type(value.tags) is FrozenList
    assert type(value.groups[0].todos[0].tags) is FrozenList
    # they are still the stdlib types for isinstance, == and serialization
    assert isinstance(value.groups, list)
    assert isinstance(value.index, dict)


def test_containers_inside_a_tuple_are_frozen(value: AgentState):
    assert isinstance(value.matrix, tuple)
    assert all(type(row) is FrozenList for row in value.matrix)
    with pytest.raises(FrozenError):
        value.matrix[0].append(9)


def test_field_assignment_raises(value: AgentState):
    with pytest.raises(FrozenError, match="mutate it inside ref.mutate"):
        value.model = "x"
    with pytest.raises(FrozenError):
        value.groups[0].name = "y"
    with pytest.raises(FrozenError):
        value.groups[0].todos[0].done = True


@pytest.mark.parametrize(
    "call",
    [
        lambda l: l.append(1),
        lambda l: l.extend([1]),
        lambda l: l.insert(0, 1),
        lambda l: l.pop(),
        lambda l: l.remove(1),
        lambda l: l.clear(),
        lambda l: l.sort(),
        lambda l: l.reverse(),
        lambda l: l.__setitem__(0, 1),
        lambda l: l.__delitem__(0),
        lambda l: l.__iadd__([1]),
        lambda l: l.__imul__(2),
        lambda l: l.__setitem__(slice(0, 1), [1]),
        lambda l: random.shuffle(l),
    ],
)
def test_every_blocked_list_method_raises(call):
    frozen = FrozenList([1, 2, 3])
    with pytest.raises(FrozenError):
        call(frozen)


@pytest.mark.parametrize(
    "call",
    [
        lambda d: d.__setitem__("a", 1),
        lambda d: d.__delitem__("a"),
        lambda d: d.__ior__({"b": 2}),
        lambda d: d.update({"b": 2}),
        lambda d: d.pop("a"),
        lambda d: d.popitem(),
        lambda d: d.setdefault("b", 2),
        lambda d: d.clear(),
    ],
)
def test_every_blocked_dict_method_raises(call):
    frozen = FrozenDict({"a": 1})
    with pytest.raises(FrozenError):
        call(frozen)


def test_non_mutating_operations_still_work():
    assert FrozenList([1, 2]) + [3] == [1, 2, 3]
    assert FrozenDict({"a": 1}) | {"b": 2} == {"a": 1, "b": 2}
    assert FrozenList([1, 2])[0] == 1


def test_frozen_containers_are_hashable(value: AgentState):
    assert isinstance(hash(value), int)
    assert isinstance(hash(value.groups), int)
    assert isinstance(hash(value.groups[0].meta), int)


def test_committed_values_are_frozen_too():
    ref = StateHost().state("a", AgentState())
    with ref.mutate() as d:
        d.groups.append(Group(name="g"))
        d.groups[0].todos.append(Todo(id="t"))
        d.index["k"] = [Todo(id="u")]
        d.tags.append("x")
    current = ref.current
    assert type(current.groups) is FrozenList
    assert type(current.groups[0].todos) is FrozenList
    assert type(current.index) is FrozenDict
    assert type(current.index["k"]) is FrozenList
    assert type(current.tags) is FrozenList
    with pytest.raises(FrozenError):
        current.groups[0].todos.append(Todo(id="z"))
