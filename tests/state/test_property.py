# ABOUTME: The core guarantee, under hypothesis — applying every patch for a state in version
# order to its version-0 snapshot yields a document equal to current.model_dump(mode="json").
# This is the property the whole streaming story rests on; if it fails, a UI built on the op
# stream is silently wrong.

"""The core guarantee: the op stream replays exactly into the committed value.

Invariant (design §7): applying every ``StatePatch`` for a ``state_id`` in version
order to the version-0 ``StateSnapshot`` yields a document equal to
``current.model_dump(mode="json")`` at each version.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from temporal_agent_harness.harness.state import DraftAliasError, HarnessState

from .support import StateHost
from .models import AgentState, Flat, Group, Priority, Todo
from .replay import apply_ops, replay

# --------------------------------------------------------------------------- #
# strategies                                                                   #
# --------------------------------------------------------------------------- #

text = st.text(max_size=8)
# Dict KEYS only (values stay plain `text`). "-" is excluded because our jsonpatch oracle
# cannot replay a `replace` at that token even against an object, though the op is valid and
# the harness emits it correctly — see test_a_dict_key_named_dash_emits_an_ordinary_replace.
dict_keys = text.filter(lambda k: k != "-")
ints = st.integers(min_value=-1000, max_value=1000)
floats = st.floats(allow_nan=False, allow_infinity=False, width=32)
tags = st.lists(text, max_size=3)


def todos(draw) -> Todo:
    return Todo(
        id=draw(text),
        text=draw(text),
        done=draw(st.booleans()),
        tags=draw(tags),
        priority=draw(st.sampled_from(list(Priority))),
    )


def groups(draw) -> Group:
    return Group(
        name=draw(text),
        todos=[todos(draw) for _ in range(draw(st.integers(0, 3)))],
        meta=draw(st.dictionaries(dict_keys, text, max_size=3)),
    )


@st.composite
def agent_states(draw) -> AgentState:
    return AgentState(
        model=draw(text),
        count=draw(ints),
        ratio=draw(st.none() | floats),
        kind=draw(st.sampled_from(["a", "b"])),
        groups=[groups(draw) for _ in range(draw(st.integers(0, 3)))],
        index=draw(
            st.dictionaries(dict_keys, st.lists(st.builds(Todo, id=text), max_size=2), max_size=2)
        ),
        scores=draw(st.dictionaries(ints, ints, max_size=3)),
        tags=draw(tags),
        pair=(draw(ints), draw(text)),
        matrix=tuple(draw(st.lists(st.lists(ints, max_size=2), max_size=2))),
    )


# --------------------------------------------------------------------------- #
# the random operation applier                                                 #
# --------------------------------------------------------------------------- #


def _apply_one(draw, d: AgentState) -> None:
    """Perform one randomly chosen mutation on the draft."""
    choices = [
        "model",
        "count",
        "ratio",
        "kind",
        "pair",
        "matrix",
        "tags_append",
        "tags_remove",
        "tags_extend",
        "tags_iadd",
        "tags_clear",
        "groups_append",
        "groups_replace",
        "groups_clear",
        "groups_extend",
        "index_set",
        "index_clear",
        "scores_set",
        "scores_update",
    ]
    if len(d.groups):
        choices += [
            "group_name",
            "group_meta_set",
            "group_meta_update",
            "group_meta_setdefault",
            "group_meta_pop",
            "group_meta_clear",
            "groups_del",
            "groups_pop",
            "groups_insert",
            "groups_setitem",
            "groups_reverse",
            "groups_sort",
            "groups_slice",
            "todo_append",
            "todo_insert",
            "todo_extend",
            "todos_clear",
        ]
        if any(len(g.todos) for g in d.groups):
            choices += [
                "todo_done",
                "todo_text",
                "todo_tags",
                "todo_setitem",
                "todo_del",
                "todo_pop",
                "todo_remove",
                "todos_sort",
                "todos_reverse",
                "todos_slice",
            ]
    if len(d.index):
        choices += ["index_append", "index_del", "index_pop", "index_nested_set"]
    if len(d.scores):
        choices += ["scores_del", "scores_popitem"]

    which = draw(st.sampled_from(sorted(choices)))

    def a_group():
        return d.groups[draw(st.integers(0, len(d.groups) - 1))]

    def a_group_with_todos():
        indices = [i for i, g in enumerate(d.groups) if len(g.todos)]
        return d.groups[draw(st.sampled_from(indices))]

    match which:
        case "model":
            d.model = draw(text)
        case "count":
            d.count = draw(ints)
        case "ratio":
            d.ratio = draw(st.none() | floats)
        case "kind":
            d.kind = draw(st.sampled_from(["a", "b"]))
        case "pair":
            d.pair = (draw(ints), draw(text))
        case "matrix":
            d.matrix = tuple(draw(st.lists(st.lists(ints, max_size=2), max_size=2)))
        case "tags_append":
            d.tags.append(draw(text))
        case "tags_remove":
            if len(d.tags):
                del d.tags[draw(st.integers(0, len(d.tags) - 1))]
        case "tags_extend":
            d.tags.extend(draw(tags))
        case "tags_iadd":
            d.tags += draw(tags)
        case "tags_clear":
            d.tags.clear()
        case "groups_append":
            d.groups.append(groups(draw))
        case "groups_extend":
            d.groups.extend([groups(draw) for _ in range(draw(st.integers(0, 2)))])
        case "groups_replace":
            d.groups = [groups(draw) for _ in range(draw(st.integers(0, 2)))]
        case "groups_clear":
            d.groups.clear()
        case "groups_del":
            del d.groups[draw(st.integers(-len(d.groups), len(d.groups) - 1))]
        case "groups_pop":
            d.groups.pop(draw(st.integers(-len(d.groups), len(d.groups) - 1)))
        case "groups_insert":
            d.groups.insert(draw(st.integers(-4, 4)), groups(draw))
        case "groups_setitem":
            d.groups[draw(st.integers(0, len(d.groups) - 1))] = groups(draw)
        case "groups_reverse":
            d.groups.reverse()
        case "groups_sort":
            d.groups.sort(key=lambda g: g.name)
        case "groups_slice":
            d.groups[0:1] = [groups(draw) for _ in range(draw(st.integers(0, 2)))]
        case "group_name":
            a_group().name = draw(text)
        case "group_meta_set":
            a_group().meta[draw(dict_keys)] = draw(text)
        case "group_meta_update":
            a_group().meta.update(draw(st.dictionaries(dict_keys, text, max_size=2)))
        case "group_meta_setdefault":
            a_group().meta.setdefault(draw(dict_keys), draw(text))
        case "group_meta_pop":
            a_group().meta.pop(draw(dict_keys), None)
        case "group_meta_clear":
            a_group().meta.clear()
        case "todo_append":
            a_group().todos.append(todos(draw))
        case "todo_insert":
            a_group().todos.insert(draw(st.integers(-4, 4)), todos(draw))
        case "todo_extend":
            a_group().todos.extend([todos(draw) for _ in range(draw(st.integers(0, 2)))])
        case "todos_clear":
            a_group().todos.clear()
        case "todo_done":
            group = a_group_with_todos()
            group.todos[draw(st.integers(0, len(group.todos) - 1))].done = draw(st.booleans())
        case "todo_text":
            group = a_group_with_todos()
            group.todos[draw(st.integers(0, len(group.todos) - 1))].text = draw(text)
        case "todo_tags":
            group = a_group_with_todos()
            group.todos[draw(st.integers(0, len(group.todos) - 1))].tags.append(draw(text))
        case "todo_setitem":
            group = a_group_with_todos()
            group.todos[draw(st.integers(0, len(group.todos) - 1))] = todos(draw)
        case "todo_del":
            group = a_group_with_todos()
            del group.todos[draw(st.integers(0, len(group.todos) - 1))]
        case "todo_pop":
            group = a_group_with_todos()
            group.todos.pop(draw(st.integers(-len(group.todos), len(group.todos) - 1)))
        case "todo_remove":
            group = a_group_with_todos()
            group.todos.remove(group.todos[draw(st.integers(0, len(group.todos) - 1))])
        case "todos_sort":
            a_group_with_todos().todos.sort(key=lambda t: t.id)
        case "todos_reverse":
            a_group_with_todos().todos.reverse()
        case "todos_slice":
            a_group_with_todos().todos[0:2] = [
                todos(draw) for _ in range(draw(st.integers(0, 2)))
            ]
        case "index_set":
            d.index[draw(dict_keys)] = [
                Todo(id=draw(text)) for _ in range(draw(st.integers(0, 2)))
            ]
        case "index_clear":
            d.index.clear()
        case "index_append":
            key = draw(st.sampled_from(sorted(d.index)))
            d.index[key].append(Todo(id=draw(text)))
        case "index_nested_set":
            key = draw(st.sampled_from(sorted(d.index)))
            if len(d.index[key]):
                d.index[key][draw(st.integers(0, len(d.index[key]) - 1))].done = draw(
                    st.booleans()
                )
        case "index_del":
            del d.index[draw(st.sampled_from(sorted(d.index)))]
        case "index_pop":
            d.index.pop(draw(st.sampled_from(sorted(d.index))), None)
        case "scores_set":
            d.scores[draw(ints)] = draw(ints)
        case "scores_update":
            d.scores.update(draw(st.dictionaries(ints, ints, max_size=2)))
        case "scores_del":
            del d.scores[draw(st.sampled_from(sorted(d.scores)))]
        case "scores_popitem":
            d.scores.popitem()
        case _:  # pragma: no cover - the match above is exhaustive
            raise AssertionError(which)


# --------------------------------------------------------------------------- #
# the property                                                                 #
# --------------------------------------------------------------------------- #


@given(st.data())
def test_patches_replay_into_the_committed_value(data):
    initial = data.draw(agent_states())
    events: list = []
    ref = StateHost(events.append).state("agent", initial)

    document = events[0].value
    assert document == ref.current.model_dump(mode="json")

    for _ in range(data.draw(st.integers(1, 4))):  # several commits
        before = ref.version
        with ref.mutate() as d:
            for _ in range(data.draw(st.integers(0, 6))):
                _apply_one(data.draw, d)

        if ref.version == before:  # nothing was touched: no event, no version bump
            assert len(events) == before + 1
            continue

        patch = events[-1]
        assert patch.version == ref.version == before + 1
        document = apply_ops(document, patch.ops)
        assert document == ref.current.model_dump(mode="json")

    # the same holds for the whole stream applied to the version-0 snapshot
    assert replay(events) == ref.current.model_dump(mode="json")


@settings(max_examples=100)
@given(st.data())
def test_set_also_replays(data):
    events: list = []
    ref = StateHost(events.append).state("agent", data.draw(agent_states()))
    document = events[0].value
    for _ in range(data.draw(st.integers(1, 3))):
        ref.set(data.draw(agent_states()))
        document = apply_ops(document, events[-1].ops)
        assert document == ref.current.model_dump(mode="json")


def test_a_dict_key_named_dash_emits_an_ordinary_replace():
    """KNOWN GOTCHA, pinned here: a mapping key that is literally ``-``.

    RFC 6901 gives ``-`` its "one past the last array element" meaning ONLY when the value
    it indexes is an array; against an object it is an ordinary member name, and there is
    no escape for it (only ``~`` and ``/`` have one). The harness gets this right — the op
    below is exactly what it should emit, and `ui/src/lib/state/jsonPatch.ts` replays it
    correctly because it checks `Array.isArray(parent)` before treating ``-`` specially.

    What cannot replay it is our test ORACLE: jsonpatch 1.33's ``ReplaceOperation.apply``
    raises on a final ``-`` token before checking whether the parent is a sequence (its own
    ``AddOperation`` checks first, so ``add`` and ``remove`` on such a key are fine). That
    is why ``dict_keys`` below excludes ``-`` — the exclusion is an oracle limitation, NOT
    a gap in the state layer, and this test is what keeps those two apart. Nothing here
    calls ``replay()``; there is no workaround in ``replay.py`` and deliberately so."""
    events: list = []
    initial = AgentState(groups=[Group(name="g", meta={"-": "before"})])
    ref = StateHost(events.append).state("a", initial)
    with ref.mutate() as d:
        d.groups[0].meta["-"] = "after"

    assert events[-1].ops == [{"op": "replace", "path": "/groups/0/meta/-", "value": "after"}]
    assert ref.current.model_dump(mode="json")["groups"][0]["meta"] == {"-": "after"}


# --------------------------------------------------------------------------- #
# deterministic regressions for the tricky sequential cases                    #
# --------------------------------------------------------------------------- #


def test_append_then_mutate_the_appended_element():
    events: list = []
    ref = StateHost(events.append).state("a", AgentState(groups=[Group(name="g")]))
    with ref.mutate() as d:
        d.groups[0].todos.append(Todo(id="t1"))
        d.groups[0].todos[0].done = True
        d.groups[0].todos[0].tags.append("x")
    assert [op["path"] for op in events[-1].ops] == [
        "/groups/0/todos/-",
        "/groups/0/todos/0/done",
        # An append, not a re-send: this read `/groups/0/todos/0/tags` when `tags` was a set,
        # because a set had no op of its own and could only be replaced whole.
        "/groups/0/todos/0/tags/-",
    ]
    # the `add` carries the pre-mutation value; the later ops finish the job
    assert events[-1].ops[0]["value"]["done"] is False
    assert replay(events) == ref.current.model_dump(mode="json")


def test_insert_rekeys_live_child_drafts():
    events: list = []
    ref = StateHost(events.append).state(
        "a",
        AgentState(groups=[Group(name="g", todos=[Todo(id="a"), Todo(id="b")])]),
    )
    with ref.mutate() as d:
        todos_ = d.groups[0].todos
        second = todos_[1]  # drafted at index 1
        todos_.insert(0, Todo(id="new"))  # now lives at index 2
        second.done = True
    assert [op["path"] for op in events[-1].ops] == [
        "/groups/0/todos/0",
        "/groups/0/todos/2/done",
    ]
    assert ref.current.groups[0].todos[2].id == "b"
    assert ref.current.groups[0].todos[2].done is True
    assert replay(events) == ref.current.model_dump(mode="json")


def test_delete_rekeys_live_child_drafts():
    events: list = []
    ref = StateHost(events.append).state(
        "a",
        AgentState(
            groups=[Group(name="g", todos=[Todo(id="a"), Todo(id="b"), Todo(id="c")])]
        ),
    )
    with ref.mutate() as d:
        todos_ = d.groups[0].todos
        third = todos_[2]
        del todos_[0]
        third.done = True
    assert [op["path"] for op in events[-1].ops] == [
        "/groups/0/todos/0",
        "/groups/0/todos/1/done",
    ]
    assert ref.current.groups[0].todos[1].id == "c"
    assert replay(events) == ref.current.model_dump(mode="json")


def test_whole_subtree_replace_is_one_op():
    events: list = []
    ref = StateHost(events.append).state("a", AgentState(groups=[Group(name="old")]))
    with ref.mutate() as d:
        d.groups = [Group(name="new")]
    assert events[-1].ops == [
        {"op": "replace", "path": "/groups", "value": [{"name": "new", "todos": [], "meta": {}}]}
    ]
    assert replay(events) == ref.current.model_dump(mode="json")


def test_in_place_operators_go_through_the_container():
    events: list = []
    ref = StateHost(events.append).state("a", AgentState())
    with ref.mutate() as d:
        d.groups += [Group(name="x"), Group(name="y")]
        d.tags += ["p", "q"]
        del d.tags[0]
    # `+=` is `extend`, so one `add` per element rather than a re-send of the list; a delete is
    # one `remove` at the index it emptied. This is what sets could never do — every change to
    # one published the whole collection — and is why they are no longer state.
    assert [op["path"] for op in events[-1].ops] == [
        "/groups/-",
        "/groups/-",
        "/tags/-",
        "/tags/-",
        "/tags/0",
    ]
    assert [op["op"] for op in events[-1].ops] == ["add", "add", "add", "add", "remove"]
    assert ref.current.tags == ["q"]
    assert replay(events) == ref.current.model_dump(mode="json")


def test_structural_list_ops_emit_whole_list_replaces():
    events: list = []
    ref = StateHost(events.append).state(
        "a", AgentState(groups=[Group(name="b"), Group(name="a")])
    )
    with ref.mutate() as d:
        d.groups.sort(key=lambda g: g.name)
    assert events[-1].ops[0]["op"] == "replace"
    assert events[-1].ops[0]["path"] == "/groups"
    assert [g["name"] for g in events[-1].ops[0]["value"]] == ["a", "b"]
    assert replay(events) == ref.current.model_dump(mode="json")


def test_nested_optional_and_union_fields():
    class Leaf(HarnessState):
        n: int = 0

    class Root(HarnessState):
        maybe: Leaf | None = None
        mixed: list[int] | dict[str, int] = []

    events: list = []
    ref = StateHost(events.append).state("r", Root())
    with ref.mutate() as d:
        d.maybe = Leaf(n=1)
    with ref.mutate() as d:
        d.maybe.n = 2
        d.mixed.append(5)
    assert ref.current.maybe.n == 2
    assert ref.current.mixed == [5]
    assert replay(events) == ref.current.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# cross-process determinism (ops must be a pure function of the mutations)      #
# --------------------------------------------------------------------------- #

# The ops are recorded IN-WORKFLOW, so two workers replaying the same history must produce
# byte-identical ops. The classic way to break that is to read a container in CPython's hash
# order, because string hashing is salted per process — so the divergence is invisible within
# one process and only shows up across several, hence the subprocess sweep below.
#
# This started as a pin on `DraftSet.pop()`, which returned a different element per process and
# published a different patch for the same mutation. Sets are gone now (the schema checker
# rejects them; see test_schema), and with them every hash-ordered read in the layer. The sweep
# stays because the claim it guards got STRONGER: determinism used to hold "when the elements
# are orderable", and now it holds outright. A test that only covered the removed type would
# have left nothing watching that.

_SEEDS = ("0", "1", "2", "3", "4", "5")

_DETERMINISM_PROBE = """
import json

from tests.state.support import StateHost
from tests.state.models import AgentState, Group, Todo

WORDS = ["juliett", "alpha", "hotel", "charlie", "bravo", "golf", "echo", "delta"]

events = []
ref = StateHost(events.append).state("agent", AgentState())

# Every container the layer owns, touched every way it can be touched: string-keyed dicts
# (the shape most likely to pick up hash order), int-keyed dicts, nested models, lists built
# by append/extend/insert/sort, and a whole-subtree assignment.
with ref.mutate() as d:
    d.tags.extend(WORDS)
    d.tags.sort()
    d.groups.append(Group(name="g", meta={w: w.upper() for w in WORDS}))
    for i, w in enumerate(WORDS):
        d.groups[0].todos.append(Todo(id=w, text=w, tags=[w]))
        d.scores[i] = len(w)
    d.index["all"] = [Todo(id=w) for w in WORDS]
    del d.groups[0].meta["golf"]
    d.groups[0].todos[3].done = True

print(json.dumps({
    "ops": [e.ops for e in events if hasattr(e, "ops")],
    "snapshot": events[0].value,
    "committed": ref.current.model_dump(mode="json"),
}, sort_keys=False))
"""


def _run_under_hash_seeds(script: str, seeds=_SEEDS) -> list[dict]:
    """Run ``script`` once per PYTHONHASHSEED, in a fresh process, and parse its JSON."""
    root = pathlib.Path(__file__).resolve().parents[2]
    results = []
    for seed in seeds:
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(root)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        results.append(json.loads(result.stdout))
    return results


def test_every_op_is_byte_identical_across_processes():
    """The whole point of the layer, checked where it can actually fail.

    Ops are derived in-workflow, so a replay on a second worker must publish exactly the same
    bytes. Hash-order reads are how that breaks, and PYTHONHASHSEED only varies BETWEEN
    processes — so a single-process assertion, however thorough, cannot see it.

    The snapshot is checked alongside the ops because it is the base every patch applies to: a
    version-0 snapshot that differs per worker is the same defect one layer down, and `frozenset`
    fields had exactly that before sets were removed.
    """
    runs = _run_under_hash_seeds(_DETERMINISM_PROBE)
    for key in ("snapshot", "ops", "committed"):
        first = runs[0][key]
        assert all(run[key] == first for run in runs), (
            f"{key} differs across processes:\n"
            + "\n".join(f"  seed {seed}: {run[key]}" for seed, run in zip(_SEEDS, runs))
        )


# --------------------------------------------------------------------------- #
# value equality across the lazy-wrapping boundary                             #
# --------------------------------------------------------------------------- #

# Lazy wrapping replaces an element with a `Draft[C]` node the first time it is read
# (`DraftList._read` writes the wrapper back with `list.__setitem__`). Every `list`
# method that compares BY VALUE — `remove`, `index`, `count`, `in`, `==` — therefore
# compares a `Draft[C]` against a `C`, and pydantic's `BaseModel.__eq__` requires
# `self_type is other_type`, so it says "not equal" about two values that are equal.
#
# The result is a container whose answers depend on whether something ELSE read it
# earlier in the same mutate() block — which is the one thing a lazy cache may never
# leak. Nested containers are immune (DraftList/DraftDict subclass list/dict, whose
# __eq__ compares contents, not class), so this is specific to model elements.


def _one_group(*ids: str) -> AgentState:
    return AgentState(groups=[Group(name="g", todos=[Todo(id=i) for i in ids])])


def test_value_equality_survives_lazy_wrapping():
    """`remove`/`index`/`count`/`in` must not care whether the list was read first."""
    ref = StateHost().state("a", _one_group("a", "b", "c"))
    target = ref.current.groups[0].todos[0]

    with ref.mutate() as d:
        todos_ = d.groups[0].todos
        assert target in todos_  # nothing has been read yet: the at-rest value matches
        _ = todos_[0]  # the read that wraps element 0 in Draft[Todo]
        assert target in todos_
        assert todos_.index(target) == 0
        assert todos_.count(target) == 1
        todos_.remove(target)

    assert [t.id for t in ref.current.groups[0].todos] == ["b", "c"]


def test_reading_a_list_does_not_change_what_it_equals():
    """`d.todos == [...]` is a value comparison; a read is not a value change."""
    ref = StateHost().state("a", _one_group("a", "b"))
    expected = [Todo(id="a"), Todo(id="b")]

    with ref.mutate() as d:
        todos_ = d.groups[0].todos
        assert todos_ == expected
        _ = todos_[0]
        assert todos_ == expected
        assert ref.current.groups[0].todos == todos_


def test_containment_guard_does_not_append_a_duplicate_after_a_read():
    """The silent shape of the defect: `in` lies, so a dedup guard writes a dupe.

    No exception and no replay divergence — the committed tree simply differs
    depending on whether an earlier pass iterated the list.
    """
    committed = []
    for read_first in (False, True):
        ref = StateHost().state("a", _one_group("a", "b"))
        with ref.mutate() as d:
            todos_ = d.groups[0].todos
            if read_first:
                for _ in todos_:  # e.g. a rendering/logging pass earlier in the turn
                    pass
            incoming = Todo(id="a")  # already present
            if incoming not in todos_:
                todos_.append(incoming)
        committed.append([t.id for t in ref.current.groups[0].todos])
    assert committed[0] == committed[1] == ["a", "b"]


def test_nested_containers_are_immune_to_lazy_wrapping():
    """The counter-case that localises the defect: only model elements are affected."""

    class Nest(HarnessState):
        rows: list[list[int]] = []
        maps: list[dict[str, int]] = []
        nums: list[int] = []

    events: list = []
    ref = StateHost(events.append).state(
        "n", Nest(rows=[[1, 2], [3, 4]], maps=[{"a": 1}, {"b": 2}], nums=[1, 2, 3])
    )
    with ref.mutate() as d:
        _ = d.rows[0]  # now a DraftList, still == [1, 2]
        _ = d.maps[0]
        assert [1, 2] in d.rows
        assert d.rows.index([3, 4]) == 1
        d.rows.remove([3, 4])
        d.maps.remove({"a": 1})
        d.nums.remove(2)
    assert [list(r) for r in ref.current.rows] == [[1, 2]]
    assert [dict(m) for m in ref.current.maps] == [{"b": 2}]
    assert list(ref.current.nums) == [1, 3]
    assert replay(events) == ref.current.model_dump(mode="json")


# --------------------------------------------------------------------------- #
# substitutability: a draft IS the value it drafts (D1)                        #
# --------------------------------------------------------------------------- #


def test_a_draft_equals_its_at_rest_value_in_both_directions():
    """`Draft[C]` is a real subclass of `C`; equality says so too.

    Both directions matter and only one of them is obvious: with the at-rest value on
    the left, Python still dispatches to the draft's `__eq__` first, because the draft's
    type is a proper subclass of the at-rest type.
    """
    ref = StateHost().state("a", _one_group("a"))
    at_rest = ref.current.groups[0].todos[0]

    with ref.mutate() as d:
        drafted = d.groups[0].todos[0]
        assert drafted == at_rest
        assert at_rest == drafted
        assert not (drafted != at_rest)


def test_a_draft_stops_equalling_the_value_once_it_is_changed():
    """Equality is about the current value, not about where the draft came from.

    This is why comparing against `meta.original` is the wrong fix: `original` is stale
    the moment the author touches the node.
    """
    ref = StateHost().state("a", _one_group("a"))
    at_rest = ref.current.groups[0].todos[0]

    with ref.mutate() as d:
        drafted = d.groups[0].todos[0]
        drafted.done = True
        assert drafted != at_rest
        assert drafted == Todo(id="a", done=True)


def test_a_draft_is_not_equal_to_a_different_state_class():
    ref = StateHost().state("a", _one_group("a"))
    with ref.mutate() as d:
        assert d.groups[0] != Todo(id="a")
        assert Todo(id="a") != d.groups[0]


def test_a_draft_hashes_as_the_value_it_drafts():
    """`__eq__` without `__hash__` would have made every draft unhashable."""
    ref = StateHost().state("f", Flat(a=1, b="x"))
    at_rest = ref.current

    with ref.mutate() as d:
        assert hash(d) == hash(at_rest)
        assert d in {at_rest: "found"}


def test_substitutable_equality_does_not_blind_the_aliasing_guard():
    """The guard is class-based, so making drafts compare equal costs it nothing.

    Spelled out because the original write-up assumed the opposite, and it is the one
    thing that would have made this fix a bad trade.
    """
    ref = StateHost().state("a", _one_group("a"))
    with pytest.raises(DraftAliasError):
        with ref.mutate() as d:
            d.groups.append(Group(name="copy", todos=[d.groups[0].todos[0]]))
