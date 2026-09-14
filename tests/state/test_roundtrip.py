# ABOUTME: copy/deepcopy/pickle/model_copy/JSON round-trips on at-rest values, and the
# order-stable set serialization that makes a whole-set `replace` op match a snapshot exactly.

"""copy / pickle / model_copy / JSON round-trips on at-rest values."""

from __future__ import annotations

import copy
import json
import pickle

import pytest

from temporal_agent_harness.harness.state.containers import FrozenDict, FrozenList, FrozenSet

from .models import AgentState, Group, Todo


@pytest.fixture
def value() -> AgentState:
    return AgentState(
        model="m",
        groups=[Group(name="g", todos=[Todo(id="t", tags={"a"})], meta={"k": "v"})],
        index={"a": [Todo(id="x")]},
        tags={"one", "two"},
        pair=(1, "z"),
        matrix=([1, 2], [3]),
    )


def test_deepcopy(value: AgentState):
    clone = copy.deepcopy(value)
    assert clone == value
    assert clone is not value
    assert type(clone.groups) is FrozenList
    assert type(clone.groups[0].meta) is FrozenDict
    assert type(clone.tags) is FrozenSet
    assert clone.groups[0] is not value.groups[0]


def test_shallow_copy(value: AgentState):
    assert copy.copy(value) == value
    assert copy.copy(value.groups) == value.groups


def test_pickle(value: AgentState):
    clone = pickle.loads(pickle.dumps(value))
    assert clone == value
    assert type(clone.groups) is FrozenList
    assert type(clone.index) is FrozenDict
    assert type(clone.tags) is FrozenSet


def test_pickle_containers_directly():
    for container in (FrozenList([1, 2]), FrozenDict({"a": 1}), FrozenSet({1, 2})):
        clone = pickle.loads(pickle.dumps(container))
        assert clone == container
        assert type(clone) is type(container)


def test_model_copy_deep(value: AgentState):
    clone = value.model_copy(deep=True)
    assert clone == value
    assert type(clone.groups[0].todos) is FrozenList


def test_json_round_trip(value: AgentState):
    dumped = value.model_dump_json()
    assert AgentState.model_validate_json(dumped) == value
    assert json.loads(dumped) == value.model_dump(mode="json")


def test_frozen_containers_compare_equal_to_plain_ones(value: AgentState):
    assert value.groups[0].meta == {"k": "v"}
    assert value.index["a"] == [Todo(id="x")]
    assert value.tags == {"one", "two"}
    assert FrozenList([1, 2]) == [1, 2]
    assert [1, 2] == FrozenList([1, 2])


def test_plain_json_dumps_of_frozen_containers():
    assert json.dumps({"l": FrozenList([1, 2]), "d": FrozenDict({"a": 1})}) == (
        '{"l": [1, 2], "d": {"a": 1}}'
    )


def test_set_serialization_is_order_stable():
    value = AgentState(tags={"zebra", "alpha", "mid"})
    assert value.model_dump(mode="json")["tags"] == ["alpha", "mid", "zebra"]
