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

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from temporal_agent_harness.harness.state import HarnessState

from .support import StateHost
from .models import AgentState, Group, Priority, Todo
from .replay import apply_ops, replay

# --------------------------------------------------------------------------- #
# strategies                                                                   #
# --------------------------------------------------------------------------- #

text = st.text(max_size=8)
ints = st.integers(min_value=-1000, max_value=1000)
floats = st.floats(allow_nan=False, allow_infinity=False, width=32)
tags = st.sets(text, max_size=3)


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
        meta=draw(st.dictionaries(text, text, max_size=3)),
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
            st.dictionaries(text, st.lists(st.builds(Todo, id=text), max_size=2), max_size=2)
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
        "tags_add",
        "tags_discard",
        "tags_update",
        "tags_ixor",
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
        case "tags_add":
            d.tags.add(draw(text))
        case "tags_discard":
            d.tags.discard(draw(text))
        case "tags_update":
            d.tags.update(draw(tags))
        case "tags_ixor":
            d.tags ^= draw(tags)
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
            a_group().meta[draw(text)] = draw(text)
        case "group_meta_update":
            a_group().meta.update(draw(st.dictionaries(text, text, max_size=2)))
        case "group_meta_setdefault":
            a_group().meta.setdefault(draw(text), draw(text))
        case "group_meta_pop":
            a_group().meta.pop(draw(text), None)
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
            group.todos[draw(st.integers(0, len(group.todos) - 1))].tags.add(draw(text))
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
            d.index[draw(text)] = [Todo(id=draw(text)) for _ in range(draw(st.integers(0, 2)))]
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


# --------------------------------------------------------------------------- #
# deterministic regressions for the tricky sequential cases                    #
# --------------------------------------------------------------------------- #


def test_append_then_mutate_the_appended_element():
    events: list = []
    ref = StateHost(events.append).state("a", AgentState(groups=[Group(name="g")]))
    with ref.mutate() as d:
        d.groups[0].todos.append(Todo(id="t1"))
        d.groups[0].todos[0].done = True
        d.groups[0].todos[0].tags.add("x")
    assert [op["path"] for op in events[-1].ops] == [
        "/groups/0/todos/-",
        "/groups/0/todos/0/done",
        "/groups/0/todos/0/tags",
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
        d.tags |= {"p", "q"}
        d.tags -= {"p"}
    # `+=` is `extend` (one add per element); `|=`/`-=` are whole-set replaces
    assert [op["path"] for op in events[-1].ops] == [
        "/groups/-",
        "/groups/-",
        "/tags",
        "/tags",
    ]
    assert [op["op"] for op in events[-1].ops] == ["add", "add", "replace", "replace"]
    assert ref.current.tags == {"q"}
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
