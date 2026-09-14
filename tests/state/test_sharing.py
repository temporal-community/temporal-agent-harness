# ABOUTME: Structural sharing — a commit rebuilds only what was touched, so an untouched
# subtree comes back by identity and holding an old value across commits stays cheap.

"""Structural sharing: a commit rebuilds only what was touched."""

from __future__ import annotations

from temporal_agent_harness.harness.state import HarnessState

from .support import StateHost
from .models import AgentState, Group, Todo


def _seeded():
    ref = StateHost().state(
        "agent",
        AgentState(
            groups=[
                Group(name=f"g{i}", todos=[Todo(id=f"t{i}{j}") for j in range(4)])
                for i in range(4)
            ],
            index={"a": [Todo(id="x")]},
            tags=["one"],
        ),
    )
    return ref


def test_untouched_subtrees_are_shared_by_identity():
    ref = _seeded()
    old = ref.current
    with ref.mutate() as d:
        d.groups[3].todos[1].done = True
    new = ref.current

    assert new is not old
    assert new.groups is not old.groups
    assert new.groups[0] is old.groups[0]
    assert new.groups[1] is old.groups[1]
    assert new.groups[2] is old.groups[2]
    assert new.index is old.index
    assert new.tags is old.tags
    assert new.groups[3] is not old.groups[3]
    assert new.groups[3].todos is not old.groups[3].todos
    assert new.groups[3].todos[0] is old.groups[3].todos[0]
    assert new.groups[3].todos[1] is not old.groups[3].todos[1]
    assert new.groups[3].todos[2] is old.groups[3].todos[2]


def test_reading_a_subtree_does_not_rebuild_it():
    ref = _seeded()
    old = ref.current
    with ref.mutate() as d:
        for group in d.groups:
            for todo in group.todos:
                assert todo.done is False
        d.count = 1
    new = ref.current
    assert new.groups is old.groups
    assert new.index is old.index


def test_leaf_assignment_only_rebuilds_the_root():
    ref = _seeded()
    old = ref.current
    with ref.mutate() as d:
        d.model = "other"
    new = ref.current
    assert new.groups is old.groups
    assert new.index is old.index
    assert new.tags is old.tags
    assert new.pair is old.pair


def test_old_values_survive_later_commits():
    ref = _seeded()
    v0 = ref.current
    with ref.mutate() as d:
        d.groups[0].name = "renamed"
    v1 = ref.current
    with ref.mutate() as d:
        d.groups.clear()
    assert v0.groups[0].name == "g0"
    assert v1.groups[0].name == "renamed"
    assert ref.current.groups == []


def test_deeply_nested_sharing():
    class Leaf(HarnessState):
        n: int = 0

    class Mid(HarnessState):
        leaf: Leaf = Leaf()
        other: Leaf = Leaf()

    class Top(HarnessState):
        mid: Mid = Mid()
        spare: Mid = Mid()

    ref = StateHost().state("t", Top())
    old = ref.current
    with ref.mutate() as d:
        d.mid.leaf.n = 5
    new = ref.current
    assert new.spare is old.spare
    assert new.mid.other is old.mid.other
    assert new.mid.leaf is not old.mid.leaf
    assert new.mid.leaf.n == 5
