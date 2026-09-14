# ABOUTME: Commit/rollback/revocation/aliasing/concurrency, plus how a dirty node is rebuilt
# at commit (model_construct where provably equivalent, model_validate where validators exist).

"""Commit / rollback / revocation / aliasing / concurrency."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from temporal_agent_harness.harness.state import (
    ConcurrentMutationError,
    DraftAliasError,
    FrozenError,
    HarnessState,
    RevokedDraftError,
    StatePatch,
    StateSchemaError,
    StateSnapshot,
)
from temporal_agent_harness.harness.state.drafts import draft_class

from .replay import replay
from .support import StateHost
from .models import AgentState, Flat, Group, Inner, Outer, Todo


@pytest.fixture
def harness_and_ref():
    events: list = []
    harness = StateHost(events.append)
    ref = harness.state("agent", AgentState())
    return events, ref


def test_registration_emits_version_zero_snapshot(harness_and_ref):
    events, ref = harness_and_ref
    assert len(events) == 1
    snapshot = events[0]
    assert isinstance(snapshot, StateSnapshot)
    assert (snapshot.state_id, snapshot.version) == ("agent", 0)
    assert snapshot.value == ref.current.model_dump(mode="json")
    assert ref.version == 0
    assert ref.state_id == "agent"


def test_design_walkthrough(harness_and_ref):
    events, ref = harness_and_ref
    with ref.mutate() as d:
        d.model = "claude-opus-5"
        d.groups.append(Group(name="g"))
        d.groups[0].todos.append(Todo(id="t1", text="plan"))
        d.groups[0].todos[0].done = True
        d.groups[0].meta["last_tool"] = "bash"

    patch = events[-1]
    assert isinstance(patch, StatePatch)
    assert patch.version == 1
    assert patch.ops == [
        {"op": "replace", "path": "/model", "value": "claude-opus-5"},
        {"op": "add", "path": "/groups/-", "value": {"name": "g", "todos": [], "meta": {}}},
        {
            "op": "add",
            "path": "/groups/0/todos/-",
            "value": {"id": "t1", "text": "plan", "done": False, "tags": [], "priority": "low"},
        },
        {"op": "replace", "path": "/groups/0/todos/0/done", "value": True},
        {"op": "add", "path": "/groups/0/meta/last_tool", "value": "bash"},
    ]
    assert ref.current.model == "claude-opus-5"
    assert ref().groups[0].todos[0].done is True


def test_noop_commit_emits_nothing(harness_and_ref):
    events, ref = harness_and_ref
    before = ref.current
    with ref.mutate() as d:
        _ = d.model
        _ = d.groups
        _ = d.tags
    assert len(events) == 1
    assert ref.version == 0
    assert ref.current is before


def test_exception_rolls_everything_back(harness_and_ref):
    events, ref = harness_and_ref
    before = ref.current
    with pytest.raises(RuntimeError, match="abort"):
        with ref.mutate() as d:
            d.model = "z"
            d.groups.append(Group(name="g"))
            raise RuntimeError("abort")
    assert ref.current is before
    assert ref.current.model == "claude-sonnet-5"
    assert ref.version == 0
    assert len(events) == 1


def test_draft_is_revoked_on_exit(harness_and_ref):
    _, ref = harness_and_ref
    with ref.mutate() as d:
        groups = d.groups
        tags = d.tags
    with pytest.raises(RevokedDraftError):
        d.model = "y"
    with pytest.raises(RevokedDraftError):
        _ = d.model
    with pytest.raises(RevokedDraftError):
        groups.append(Group(name="g"))
    with pytest.raises(RevokedDraftError):
        tags.append("x")


def test_concurrent_mutation_is_rejected(harness_and_ref):
    _, ref = harness_and_ref
    with ref.mutate():
        with pytest.raises(ConcurrentMutationError):
            with ref.mutate():
                pass
    with ref.mutate() as d:  # the failed attempt did not leave the ref locked
        d.count = 1
    assert ref.current.count == 1


def test_set_replaces_the_whole_root(harness_and_ref):
    events, ref = harness_and_ref
    ref.set(AgentState(model="other", count=7))
    assert ref.version == 1
    assert events[-1].ops == [
        {"op": "replace", "path": "", "value": ref.current.model_dump(mode="json")}
    ]
    assert ref.current.model == "other"


def test_set_is_rejected_while_a_draft_is_open(harness_and_ref):
    _, ref = harness_and_ref
    with ref.mutate():
        with pytest.raises(ConcurrentMutationError):
            ref.set(AgentState())


def test_validation_error_lands_on_the_assignment_line(harness_and_ref):
    events, ref = harness_and_ref
    with pytest.raises(ValidationError):
        with ref.mutate() as d:
            d.count = "not an int"
    assert ref.version == 0
    assert len(events) == 1

    with pytest.raises(ValidationError):
        with ref.mutate() as d:
            d.groups.append(Group(name="ok"))
            d.groups.append("not a group")
    assert ref.version == 0
    assert len(events) == 1


def test_validator_failure_at_commit_leaves_current_untouched():
    from pydantic import model_validator

    class Bounded(HarnessState):
        low: int = 0
        high: int = 10

        @model_validator(mode="after")
        def _check(self):
            if self.low > self.high:
                raise ValueError("low > high")
            return self

    events: list = []
    ref = StateHost(events.append).state("b", Bounded())
    before = ref.current
    with pytest.raises(ValidationError):
        with ref.mutate() as d:
            d.low = 50  # per-field validation passes; the model validator fails later
    assert ref.current is before
    assert ref.version == 0
    assert len(events) == 1


def test_unknown_field_assignment(harness_and_ref):
    _, ref = harness_and_ref
    with pytest.raises((AttributeError, ValueError)):
        with ref.mutate() as d:
            d.nope = 1


def test_field_deletion_is_rejected(harness_and_ref):
    _, ref = harness_and_ref
    with pytest.raises(TypeError):
        with ref.mutate() as d:
            del d.model
    with pytest.raises(FrozenError):
        del ref.current.model


def test_draft_cannot_be_aliased(harness_and_ref):
    _, ref = harness_and_ref
    with pytest.raises(DraftAliasError):
        with ref.mutate() as d:
            d.groups.append(Group(name="g"))
            d.index["k"] = [Todo(id="t")]
            d.groups[0].todos.append(d.index["k"][0])

    with pytest.raises(DraftAliasError):
        with ref.mutate() as d:
            d.groups.append(Group(name="g"))
            d.groups = [d.groups[0]]


def test_at_rest_value_may_be_reassigned_anywhere(harness_and_ref):
    _, ref = harness_and_ref
    with ref.mutate() as d:
        d.groups.append(Group(name="g", todos=[Todo(id="t")]))
    todo = ref.current.groups[0].todos[0]
    with ref.mutate() as d:
        d.index["copy"] = [todo]
    assert ref.current.index["copy"][0] == todo


def test_draft_class_is_generated_once_and_lives_on_the_state_class():
    """One `Draft[C]` per declared class, reachable only from `C` itself.

    Holding it anywhere else — a module-level dict, say — outlives every class the
    Temporal sandbox re-imports per workflow instance, and that is an unbounded leak
    rather than a cache (see `test_the_draft_class_cache_does_not_grow_per_workflow_instance`).
    """
    first = draft_class(Flat)
    second = draft_class(Flat)
    assert first is second
    assert Flat.__dict__["__harness_draft_class__"] is first
    assert issubclass(first, Flat)
    assert first.__harness_at_rest__ is Flat


def test_the_draft_class_is_built_when_the_state_class_is_declared():
    """Class generation belongs at definition time, not on a workflow task."""

    class Declared(HarnessState):
        n: int = 0

    assert Declared.__dict__["__harness_draft_class__"].__name__ == "Draft[Declared]"


def test_a_subclass_gets_its_own_draft_class():
    """`cls.__dict__`, never `getattr`: an inherited draft class drops the new fields."""

    class Parent(HarnessState):
        a: int = 0

    class Child(Parent):
        b: int = 0

    assert draft_class(Child) is not draft_class(Parent)

    ref = StateHost().state("c", Child())
    with ref.mutate() as d:
        d.b = 7
    assert ref.current.b == 7


def test_draft_is_an_instance_of_the_user_class():
    ref = StateHost().state("o", Outer())
    with ref.mutate() as d:
        assert isinstance(d, Outer)
        assert isinstance(d.inner, Inner)
        d.inner.value = 3
    assert ref.current.inner.value == 3
    assert type(ref.current) is Outer
    assert type(ref.current.inner) is Inner


def test_version_only_advances_on_committing_mutations(harness_and_ref):
    _, ref = harness_and_ref
    assert ref.version == 0
    with ref.mutate() as d:
        d.count = 1
    assert ref.version == 1
    with ref.mutate():
        pass
    assert ref.version == 1
    ref.set(AgentState())
    assert ref.version == 2


def test_harness_rejects_duplicate_state_ids():
    harness = StateHost()
    harness.state("a", Flat())
    with pytest.raises(ValueError):
        harness.state("a", Flat())


def test_root_must_be_a_harness_state():
    from temporal_agent_harness.harness.state import StateRef

    with pytest.raises(TypeError):
        StateRef("x", {"not": "a model"})  # type: ignore[arg-type]


def test_repeated_assignment_emits_both_ops(harness_and_ref):
    events, ref = harness_and_ref
    with ref.mutate() as d:
        d.count = 1
        d.count = 2
    assert events[-1].ops == [
        {"op": "replace", "path": "/count", "value": 1},
        {"op": "replace", "path": "/count", "value": 2},
    ]
    assert ref.current.count == 2


# --------------------------------------------------------------------------- #
# how a dirty node is rebuilt at commit                                        #
# --------------------------------------------------------------------------- #


def test_validator_free_models_skip_commit_validation():
    from .models import Group, Todo

    assert AgentState.__harness_validate_on_commit__ is False
    assert Group.__harness_validate_on_commit__ is False
    assert Todo.__harness_validate_on_commit__ is False


def test_a_model_with_validators_validates_on_commit():
    from pydantic import field_validator

    class Positive(HarnessState):
        n: int = 1

        @field_validator("n")
        @classmethod
        def _positive(cls, value: int) -> int:
            if value <= 0:
                raise ValueError("n must be positive")
            return value

    assert Positive.__harness_validate_on_commit__ is True

    events: list = []
    ref = StateHost(events.append).state("p", Positive())
    before = ref.current
    # the field's own annotation accepts -1, so the class validator only fires
    # at commit - and the commit is then discarded whole.
    with pytest.raises(ValidationError):
        with ref.mutate() as d:
            d.n = -1
    assert ref.current is before
    assert ref.version == 0
    assert len(events) == 1

    with ref.mutate() as d:
        d.n = 5
    assert ref.current.n == 5


def test_config_affecting_validation_forces_commit_validation():
    from pydantic import ConfigDict

    class Lowered(HarnessState):
        model_config = ConfigDict(str_to_lower=True)
        s: str = ""

    assert Lowered.__harness_validate_on_commit__ is True


def test_the_commit_strategy_can_be_forced_either_way():
    from pydantic import ConfigDict, model_validator

    class Loose(HarnessState):
        model_config = ConfigDict(harness_validate_on_commit=False)
        n: int = 0

        @model_validator(mode="after")
        def _never_runs_at_commit(self):
            if self.n == 99:
                raise ValueError("boom")
            return self

    assert Loose.__harness_validate_on_commit__ is False
    ref = StateHost().state("l", Loose())
    with ref.mutate() as d:
        d.n = 99  # the model validator is skipped by explicit opt-out
    assert ref.current.n == 99

    class Strict(HarnessState):
        model_config = ConfigDict(harness_validate_on_commit=True)
        n: int = 0

    assert Strict.__harness_validate_on_commit__ is True
    ref2 = StateHost().state("s", Strict())
    with ref2.mutate() as d:
        d.n = 3
    assert ref2.current.n == 3


# --------------------------------------------------------------------------- #
# aliasing holes: a draft node reached through a model the guard does not walk  #
# --------------------------------------------------------------------------- #
#
# `assert_no_drafts` returns as soon as it meets a `BaseModel`, and `freeze()`
# returns a model untouched, so a model *built inside the block* can carry a live
# draft node straight into at-rest state.  `DraftSet._coerce` skips the guard
# entirely.  Every test below is a hole; each asserts the contract, not the bug.


def test_draft_model_cannot_be_smuggled_inside_a_newly_built_model(harness_and_ref):
    """A draft reached through a *new* model's field is still a draft."""
    _, ref = harness_and_ref
    with ref.mutate() as d:
        d.groups.append(Group(name="g", todos=[Todo(id="t1")]))
    with pytest.raises(DraftAliasError):
        with ref.mutate() as d:
            smuggled = d.groups[0].todos[0]  # Draft[Todo]
            d.groups.append(Group(name="copy", todos=[smuggled]))


def test_draft_model_cannot_be_smuggled_through_a_model_typed_field():
    """The same hole through a plain model-typed field rather than a list."""

    class Pair(HarnessState):
        left: Inner = Inner()
        right: Inner = Inner()

    class Root(HarnessState):
        pair: Pair = Pair()

    ref = StateHost().state("r", Root(pair=Pair(left=Inner(value=1))))
    with pytest.raises(DraftAliasError):
        with ref.mutate() as d:
            d.pair = Pair(left=d.pair.left, right=Inner(value=2))


def test_at_rest_state_never_holds_a_draft_node(harness_and_ref):
    """Whatever the guard does, nothing draft-flavoured may survive commit."""
    _, ref = harness_and_ref
    with ref.mutate() as d:
        d.groups.append(Group(name="g", todos=[Todo(id="t1")]))
    try:
        with ref.mutate() as d:
            d.groups.append(Group(name="copy", todos=[d.groups[0].todos[0]]))
    except DraftAliasError:
        return  # guarded at the assignment line, which is the desired behaviour
    node = list.__getitem__(ref.current.groups[1].todos, 0)
    assert not type(node).__dict__.get("__harness_is_draft__", False), (
        f"a live {type(node).__name__} is sitting in ref.current"
    )
    assert node.text == ""  # must not raise RevokedDraftError from at-rest data


def test_smuggled_draft_does_not_desynchronize_the_op_stream(harness_and_ref):
    """The patch stream must always reproduce the committed value."""
    events, ref = harness_and_ref
    with ref.mutate() as d:
        d.groups.append(Group(name="g", todos=[Todo(id="t1", text="orig")]))
    try:
        with ref.mutate() as d:
            smuggled = d.groups[0].todos[0]
            d.groups.append(Group(name="copy", todos=[smuggled]))
            smuggled.text = "changed"  # recorded for /groups/0 only
    except DraftAliasError:
        return
    assert replay(events) == ref.current.model_dump(mode="json")


def test_a_set_field_cannot_be_declared_at_all(harness_and_ref):
    """The set-shaped hole in ``assert_no_drafts`` is closed by there being no sets.

    This replaces two tests that pinned real defects: ``DraftSet._coerce`` skipped both
    ``assert_no_drafts`` and ``freeze``, so ``d.chosen.add(d.steps[0])`` put a LIVE draft into a
    set; and because ``HarnessState.__hash__`` hashes ``tuple(self.__dict__.values())``, editing
    that draft afterwards moved its hash, so the member fell out of its own bucket, re-adding it
    duplicated it, and the published patch carried a set serialized with two identical members
    while ``commit`` re-hashed it back down to one.

    None of that is fixed here — it is *unreachable*, because a class declaring a set no longer
    defines. That is the honest reason to keep this test: it pins the assumption the deletion of
    ``DraftSet`` rests on.
    """
    with pytest.raises(StateSchemaError):

        class Plan(HarnessState):
            steps: list[Todo] = []
            chosen: set[Todo] = set()


# --------------------------------------------------------------------------- #
# a commit that raises: the ref must be whole, not half-unwound                #
# --------------------------------------------------------------------------- #
#
# `_MutateContext.__exit__` revokes the session and clears `ref._open` *before*
# calling `commit()`, so a class validator firing at commit propagates out of the
# `with` from a ref that is already unlocked.  The two tests above pin that
# `current` and `version` survive it; these pin the rest of the contract, which is
# what a consumer of the patch stream actually depends on: nothing is published,
# no untouched subtree is disturbed, the next block starts from the *original*
# value, and snapshot-plus-patches still reproduces `current`.


def test_a_failed_commit_leaves_no_trace_and_the_ref_fully_usable():
    from pydantic import field_validator

    class Leaf(HarnessState):
        n: int = 0

        @field_validator("n")
        @classmethod
        def _small(cls, value: int) -> int:
            if value > 100:
                raise ValueError("n too large")
            return value

    class Branch(HarnessState):
        leaves: list[Leaf] = []
        label: str = ""

    class Tree(HarnessState):
        branch: Branch = Branch()
        note: str = ""

    events: list = []
    ref = StateHost(events.append).state(
        "tree", Tree(branch=Branch(leaves=[Leaf(n=1), Leaf(n=2)], label="L"))
    )
    original = ref.current
    original_branch = ref.current.branch
    original_leaves = ref.current.branch.leaves
    original_leaf0 = ref.current.branch.leaves[0]

    with pytest.raises(ValidationError):
        with ref.mutate() as d:
            d.note = "touched"
            d.branch.label = "touched"
            d.branch.leaves[0].n = 7  # this node commits cleanly, *before* the failure
            d.branch.leaves[1].n = 999  # only the class validator rejects it, at commit
            draft = d

    # nothing moved, by identity - so no untouched subtree was rebuilt either, and
    # the sibling that already committed left no trace.
    assert ref.current is original
    assert ref.current.branch is original_branch
    assert ref.current.branch.leaves is original_leaves
    assert ref.current.branch.leaves[0] is original_leaf0
    assert ref.current.branch.leaves[0].n == 1
    assert ref.version == 0

    # the recorded ops are dropped whole: still just the version-0 snapshot.
    assert len(events) == 1
    assert isinstance(events[0], StateSnapshot)

    # the value is still deeply frozen and the draft is still revoked.
    with pytest.raises(FrozenError):
        ref.current.note = "z"
    with pytest.raises(FrozenError):
        ref.current.branch.leaves.append(Leaf())
    with pytest.raises(RevokedDraftError):
        draft.note = "z"

    # `_open` was cleared, so the next block is not locked out - and it drafts from
    # the ORIGINAL value, so its ops carry nothing from the abandoned block.
    with ref.mutate() as d:
        d.note = "second"
    assert ref.version == 1  # 0 -> 1, not 0 -> 2
    patch = events[-1]
    assert isinstance(patch, StatePatch)
    assert patch.version == 1
    assert patch.ops == [{"op": "replace", "path": "/note", "value": "second"}]
    assert ref.current.branch.label == "L"
    assert ref.current.branch.leaves[0].n == 1

    # the stream a consumer sees still reproduces the value the ref holds.
    assert replay(events) == ref.current.model_dump(mode="json")


def test_failed_commits_never_desynchronize_the_patch_stream():
    """Failures interleaved with successes keep the versions contiguous."""
    from pydantic import field_validator

    class Counter(HarnessState):
        n: int = 0

        @field_validator("n")
        @classmethod
        def _not_thirteen(cls, value: int) -> int:
            if value == 13:
                raise ValueError("unlucky")
            return value

    events: list = []
    ref = StateHost(events.append).state("c", Counter())
    for target in (1, 13, 2, 13, 13, 3):
        try:
            with ref.mutate() as d:
                d.n = target
        except ValidationError:
            pass  # the failures must be invisible to the stream

    assert ref.current.n == 3
    assert ref.version == 3
    assert [event.version for event in events] == [0, 1, 2, 3]
    assert replay(events) == ref.current.model_dump(mode="json")
