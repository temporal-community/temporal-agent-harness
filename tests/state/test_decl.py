# ABOUTME: StateDecl — state declared as an agent class attribute. Class access gives the
# declaration and instance access the instance's own ref; the id is the attribute name; bad
# declarations fail when the class body runs; and the runner attaches a ref as it stands.

"""``agent.state(...)`` declarations: descriptor behaviour, validation, attachment."""

from __future__ import annotations

from typing import Any

import pytest

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.state import (
    HarnessState,
    StateDecl,
    StateRef,
    StateSnapshot,
    declared_states,
)

from .models import AgentState


class Counter(HarnessState):
    n: int = 0


class Named(HarnessState):
    name: str


class Probe:
    plan = agent.state(AgentState)
    counter = agent.state(Counter)


def test_class_access_is_the_declaration_and_the_attribute_name_is_the_id():
    assert isinstance(Probe.plan, StateDecl)
    assert Probe.plan.state_id == "plan"
    assert Probe.plan.state_type is AgentState


def test_instance_access_is_one_stable_ref_per_instance():
    probe = Probe()
    assert isinstance(probe.counter, StateRef)
    assert probe.counter is probe.counter
    assert probe.counter.state_id == "counter"
    assert probe.counter.current == Counter()


def test_two_instances_never_share_a_ref():
    first, second = Probe(), Probe()
    with first.counter.mutate() as d:
        d.n = 1
    assert first.counter is not second.counter
    assert second.counter.current.n == 0
    assert "__harness_state_refs__" not in vars(Probe)


def test_declared_state_cannot_be_reassigned():
    probe = Probe()
    with pytest.raises(AttributeError, match="cannot be reassigned"):
        probe.counter = Counter(n=3)  # type: ignore[assignment]


def test_a_non_harness_state_type_fails_when_the_class_body_runs():
    with pytest.raises(TypeError, match="must be a HarnessState subclass"):

        class _Bad:
            plan = agent.state(dict)  # type: ignore[type-var]


def test_a_type_with_required_fields_needs_an_initial_value():
    with pytest.raises(TypeError, match=r"required fields \['name'\].*initial="):

        class _Bad:
            who = agent.state(Named)

    class _Good:
        who = agent.state(Named, initial=lambda: Named(name="ada"))

    assert _Good().who.current.name == "ada"


def test_an_initial_of_the_wrong_type_fails_on_first_use():
    class _Wrong:
        counter = agent.state(Counter, initial=lambda: Named(name="x"))  # type: ignore[arg-type,return-value]

    with pytest.raises(TypeError, match="initial\\(\\) returned Named, not Counter"):
        _Wrong().counter


def test_one_declaration_under_two_names_fails():
    shared = agent.state(Counter)
    with pytest.raises((TypeError, RuntimeError)):

        class _Twice:
            a = shared
            b = shared


def test_an_unattached_declaration_names_the_fix():
    loose = agent.state(Counter)
    with pytest.raises(TypeError, match="declare it in the agent's class body"):
        loose.state_id


def test_declared_states_are_in_declaration_order_and_follow_overrides():
    class Base:
        plan = agent.state(AgentState)
        counter = agent.state(Counter)

    class Child(Base):
        counter = None  # type: ignore[assignment]
        extra = agent.state(Counter)

    assert list(declared_states(Base)) == ["plan", "counter"]
    assert list(declared_states(Child)) == ["plan", "extra"]


def test_attach_folds_unpublished_commits_into_the_initial_snapshot():
    probe = Probe()
    with probe.counter.mutate() as d:
        d.n = 5
    assert probe.counter.version == 1

    published: list[Any] = []
    snapshot = probe.counter._attach(published.append)
    assert snapshot == StateSnapshot(state_id="counter", version=0, value={"n": 5})
    assert published == []

    with probe.counter.mutate() as d:
        d.n = 6
    assert [(e.version, e.ops) for e in published] == [
        (1, [{"op": "replace", "path": "/n", "value": 6}])
    ]
    with pytest.raises(RuntimeError, match="already publishing"):
        probe.counter._attach(published.append)
