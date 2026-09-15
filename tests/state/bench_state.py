# ABOUTME: Performance guards for the costs the state design commits to — a leaf set and an
# untouched-subtree commit must not scale with the size of the state.

"""Regression guards for the costs the design commits to (§9).

Run with ``pytest tests/state/bench_state.py --benchmark-only``.
"""

from __future__ import annotations

import pytest

from temporal_agent_harness.harness.state import HarnessState

from .support import StateHost
from .models import AgentState, Group, Todo

N = 10_000


class Row(HarnessState):
    id: int = 0
    name: str = "x" * 64
    flag: bool = False


class Big(HarnessState):
    """Roughly a megabyte of state once the rows are materialized."""

    label: str = ""
    counter: int = 0
    rows: list[Row] = []
    lookup: dict[str, str] = {}


def _big() -> Big:
    return Big(
        rows=[Row(id=i) for i in range(N)],
        lookup={str(i): "y" * 32 for i in range(N)},
    )


@pytest.fixture(scope="module")
def big() -> Big:
    return _big()


def test_leaf_set_on_a_large_state(benchmark, big: Big):
    """One scalar assignment must not depend on the size of the state."""
    ref = StateHost().state("big", big)

    def one():
        with ref.mutate() as d:
            d.counter += 1

    benchmark(one)
    assert ref.current.rows is big.rows  # untouched subtree shared throughout


def test_append_to_a_large_list(benchmark, big: Big):
    ref = StateHost().state("big", big)

    def one():
        with ref.mutate() as d:
            d.rows.append(Row(id=-1))

    benchmark(one)


def test_commit_of_an_untouched_large_tree(benchmark, big: Big):
    """A no-op-on-the-big-part commit is O(1) in the big part."""
    ref = StateHost().state("big", big)
    rows = ref.current.rows
    lookup = ref.current.lookup

    def one():
        with ref.mutate() as d:
            d.label = "touched"

    benchmark(one)
    assert ref.current.rows is rows
    assert ref.current.lookup is lookup


def test_assign_a_large_list_of_models(benchmark, big: Big):
    """The case the draft-aliasing guard walks in full.

    Assigning a whole container is the one shape where `assert_no_drafts` is O(tree)
    rather than O(1), so it is the shape that would show it if the walk were expensive.
    It is not: the same value goes straight on to `validate_python`, which walks it too.
    """
    ref = StateHost().state("big", big)
    rows = [Row(id=i) for i in range(N)]

    def one():
        with ref.mutate() as d:
            d.rows = rows

    benchmark(one)
    assert len(ref.current.rows) == N


def test_noop_commit(benchmark, big: Big):
    ref = StateHost().state("big", big)

    def one():
        with ref.mutate():
            pass

    benchmark(one)
    assert ref.version == 0


def test_first_read_of_a_large_list_in_a_draft(benchmark, big: Big):
    """The pointer copy taken the first time a container is touched."""
    ref = StateHost().state("big", big)

    def one():
        with ref.mutate() as d:
            _ = d.rows[0]

    benchmark(one)


def test_reads_on_current_are_native(benchmark, big: Big):
    ref = StateHost().state("big", big)

    def one():
        current = ref.current
        return current.rows[N // 2].name

    benchmark(one)


def test_deep_nested_mutation(benchmark):
    ref = StateHost().state(
        "agent",
        AgentState(
            groups=[
                Group(name=f"g{i}", todos=[Todo(id=f"t{j}") for j in range(50)])
                for i in range(50)
            ]
        ),
    )
    flag = [False]

    def one():
        flag[0] = not flag[0]
        with ref.mutate() as d:
            d.groups[25].todos[25].done = flag[0]

    benchmark(one)
