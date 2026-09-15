# ABOUTME: Draft nodes — the per-node bookkeeping (DraftMeta), the generated Draft[C]
# subclass of the author's own state class, lazy wrapping of children on first read, and the
# commit that rebuilds an at-rest value while sharing every untouched subtree by identity.

"""Draft nodes: per-node bookkeeping, the generated draft subclass, wrap and commit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from .base import ANY_NODE, AdapterNode, HarnessState, bind_drafts
from .containers import (
    DraftDict,
    DraftList,
    FrozenDict,
    FrozenList,
    bind,
    freeze,
)
from .errors import DraftAliasError
from .session import DraftSession, escape

__all__ = [
    "DraftMeta",
    "draft_class",
    "build_draft_class",
    "make_model_draft",
    "wrap",
    "commit",
    "meta_of",
    "is_draft",
    "mark_dirty",
    "path_of",
    "draft_set_field",
]

C = TypeVar("C", bound=HarnessState)


@dataclass(slots=True)
class DraftMeta:
    """Bookkeeping carried by every draft node in one ``mutate()`` tree."""

    session: DraftSession
    parent: "DraftMeta | None"  # None for the root
    key: str | int | None  # pre-escaped pointer segment, or a list index
    original: Any  # the at-rest value this node was drafted from
    dirty: bool = False


# --------------------------------------------------------------------------- #
# node identification, paths, dirty propagation                                #
# --------------------------------------------------------------------------- #


def meta_of(node: Any) -> DraftMeta | None:
    """Return the ``DraftMeta`` of ``node``, or ``None`` if it is not a draft."""
    cls = type(node)
    if cls is DraftList or cls is DraftDict:
        return node._meta  # type: ignore[attr-defined]
    if isinstance(node, BaseModel) and cls.__dict__.get("__harness_is_draft__", False):
        private = object.__getattribute__(node, "__pydantic_private__")
        return private["_draft"] if private else None
    return None


def is_draft(node: Any) -> bool:
    return meta_of(node) is not None


def path_of(meta: DraftMeta) -> str:
    """RFC 6901 pointer to this node.  Segments are escaped by their producer."""
    parts: list[str] = []
    while meta.parent is not None:
        parts.append(meta.key if type(meta.key) is str else str(meta.key))
        meta = meta.parent
    if not parts:
        return ""
    parts.reverse()
    return "/" + "/".join(parts)


def mark_dirty(meta: DraftMeta | None) -> None:
    while meta is not None and not meta.dirty:
        meta.dirty = True
        meta = meta.parent


# Leaves the walk below can stop at without looking inside.  Exact types, not
# isinstance: a subclass of `str` is still a leaf, but a subclass of `list` is not.
_LEAF_TYPES = frozenset({str, int, float, bool, bytes, type(None)})


def assert_no_drafts(value: Any) -> None:
    """A draft node has exactly one parent; it may never be stored elsewhere.

    The whole value is walked, models included.  Stopping at a ``BaseModel`` used to
    leave a hole wide enough to desynchronize the stream: ``revalidate_instances="never"``
    means pydantic accepts a ``Draft[Todo]`` wherever a ``Todo`` is annotated — it is a
    real subclass — and keeps the live draft object, so
    ``Group(name="copy", todos=[draft_todo])`` carried the draft into at-rest state.
    Mutating it afterwards recorded the op against its *original* path while both
    locations shared the node by identity, and replay stopped reproducing ``current``.

    There is deliberately no fast path for ``Frozen*`` containers: ``_freeze_containers``
    runs on models built *inside* a ``mutate()`` block too, so a ``FrozenList`` is no
    evidence at all of being at rest, and the one in the repro held the draft.

    The cost is O(tree) on something the caller just constructed, and it does not change
    the asymptotics of the callers: every one of them already hands the same value to
    ``validate_python``, which walks it too.
    """
    if type(value) in _LEAF_TYPES:
        return
    if meta_of(value) is not None:
        raise DraftAliasError(
            "a draft node cannot be assigned to another location; assign a value "
            "taken from ref.current, or build a new one"
        )
    if isinstance(value, BaseModel):
        for item in value.__dict__.values():
            assert_no_drafts(item)
        extra = value.__pydantic_extra__
        if extra:
            for item in extra.values():
                assert_no_drafts(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            assert_no_drafts(item)
    elif isinstance(value, dict):
        for item in value.values():
            assert_no_drafts(item)


# --------------------------------------------------------------------------- #
# model drafts                                                                 #
# --------------------------------------------------------------------------- #


class _ModelDraftMixin:
    """Precedes the user's class in the MRO so field access is intercepted.

    Only field reads pay the check; everything else falls through to
    ``object.__getattribute__`` after a single ``startswith`` test.
    """

    def __getattribute__(self, name: str) -> Any:
        ga = object.__getattribute__
        if name.startswith("_") or name not in ga(self, "__harness_field_names__"):
            return ga(self, name)
        value = ga(self, name)
        meta = ga(self, "__pydantic_private__")["_draft"]
        meta.session.check()
        if not wrappable(value):
            return value
        node = ga(self, "__harness_adapters__")[name]
        wrapped = wrap(value, node, meta, escape(name))
        if wrapped is not value:
            ga(self, "__dict__")[name] = wrapped
        return wrapped

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            return BaseModel.__setattr__(self, name, value)  # type: ignore[misc]
        meta = object.__getattribute__(self, "__pydantic_private__")["_draft"]
        draft_set_field(self, meta, name, value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            return BaseModel.__delattr__(self, name)  # type: ignore[misc]
        raise TypeError(
            f"cannot delete field {name!r}: state fields are declared by the schema"
        )

    def __eq__(self, other: object) -> bool:
        """A draft equals the value it drafts.  ``Draft[C]`` is substitutable for ``C``.

        Pydantic's ``BaseModel.__eq__`` requires ``type(a) is type(b)``, which is exactly
        wrong here, because a draft node appears *inside* a container the author is still
        reading: ``DraftList._read`` swaps an element for its draft wrapper in place, so
        every by-value list method — ``in``, ``count``, ``index``, ``remove``, ``==`` —
        was comparing a ``Draft[C]`` against a ``C`` and being told they differ.  The
        loud half raised; the quiet half just answered wrongly, so

            if incoming not in d.todos: d.todos.append(incoming)

        appended a duplicate whenever something earlier in the turn had iterated the
        list, with no exception and no replay divergence to show for it.

        Comparing against the at-rest class fixes every one of those at once, including
        ``d.todos == [...]`` and ``DraftDict.__eq__``, because ``Draft[C]`` is a proper
        subclass of ``C`` and Python therefore gives this method priority even when the
        at-rest value is the left operand.  Nothing in the layer detects a draft by
        comparison — ``meta_of``, ``wrap``, ``commit`` and ``assert_no_drafts`` all test
        ``__harness_is_draft__``, and the two write-back guards use ``is`` — so no guard
        loses anything to this.

        ``__pydantic_private__`` is deliberately not compared: on a draft it holds the
        ``DraftMeta``, which is bookkeeping, not value.  The schema checker forbids every
        other private attribute, so there is nothing else in there to miss.
        """
        if self is other:
            return True
        ga = object.__getattribute__
        at_rest = ga(self, "__harness_at_rest__")
        other_cls = type(other)
        if other_cls.__dict__.get("__harness_at_rest__", other_cls) is not at_rest:
            return NotImplemented
        return bool(
            ga(self, "__dict__") == ga(other, "__dict__")
            and ga(self, "__pydantic_extra__") == ga(other, "__pydantic_extra__")
        )

    def __hash__(self) -> int:
        """Hashed as the value it drafts, so ``__eq__`` and ``__hash__`` agree.

        Defined explicitly because a class that defines ``__eq__`` and not ``__hash__``
        gets ``__hash__ = None`` and becomes unhashable — which would take away
        something drafts of scalar-only models can do today.

        A draft of a model with a container field stays unhashable, as it was before:
        ``DraftList`` inherits ``list.__hash__``, which is ``None``.  That is the honest
        answer for a value the author is in the middle of changing — a hash taken at the
        top of a ``mutate()`` block would not survive to the bottom of it.
        """
        ga = object.__getattribute__
        return hash((ga(self, "__harness_at_rest__"), tuple(ga(self, "__dict__").values())))


def build_draft_class(cls: type[C]) -> type[C]:
    """Generate ``Draft[C]`` and hang it off ``C`` itself.

    Called once per ``HarnessState`` subclass, from ``__pydantic_init_subclass__`` —
    i.e. at class-definition time, on the thread that is defining the class, and never
    on a workflow task.

    The generated class is stored **on the state class** rather than in a module-level
    cache, and that is the whole point: a module-level dict keyed on ``cls`` outlives
    every class the Temporal sandbox re-imports per workflow instance, so a state class
    declared next to its ``@workflow.defn`` leaks one entry (plus its draft subclass and
    both pydantic cores, ~107 kB RSS) per workflow *run*, forever.  ``C`` and ``Draft[C]``
    referring to each other is one ordinary gc-collectable cycle that dies with the
    re-imported module.
    """
    generated = type(
        f"Draft[{cls.__name__}]",
        (_ModelDraftMixin, cls),
        {
            "__harness_is_draft__": True,
            "__harness_at_rest__": cls,
            "__module__": cls.__module__,
            "__qualname__": f"Draft[{cls.__qualname__}]",
        },
    )
    setattr(cls, "__harness_draft_class__", generated)
    return generated  # type: ignore[return-value]


def draft_class(cls: type[C]) -> type[C]:
    """The generated ``Draft[C]`` subclass of ``C``.

    ``cls.__dict__`` and never ``getattr``: a subclass inherits its parent's draft class
    attribute, and drafting a ``Child`` through ``Draft[Parent]`` would silently drop
    every field ``Child`` added.  Normally the lookup hits, because the class was built
    when ``C`` was defined; the fallback covers a class that somehow reached here without
    the definition-time hook, and stores its result the same way.
    """
    generated = cls.__dict__.get("__harness_draft_class__")
    if generated is None:
        generated = build_draft_class(cls)
    return generated  # type: ignore[return-value]


def make_model_draft(
    original: C,
    session: DraftSession,
    parent: DraftMeta | None,
    key: str | int | None,
) -> C:
    cls = draft_class(type(original))
    node = cls.model_construct(**original.__dict__)
    object.__setattr__(
        node,
        "__pydantic_private__",
        {"_draft": DraftMeta(session, parent, key, original)},
    )
    return node  # type: ignore[return-value]


def draft_set_field(obj: Any, meta: DraftMeta, name: str, value: Any) -> None:
    meta.session.check()
    cls = type(obj)
    if name not in cls.model_fields:
        raise AttributeError(f"{cls.__name__} has no field {name!r}")
    data = object.__getattribute__(obj, "__dict__")
    if value is data.get(name) and meta_of(value) is not None:
        # `d.xs += [...]` / `d.tags |= {...}`: the in-place operator already ran on
        # the draft container and recorded its ops; Python is only storing it back.
        return
    assert_no_drafts(value)
    node: AdapterNode = cls.__harness_adapters__[name]
    validated = node.adapter.validate_python(value)  # raises at the assignment line
    frozen = freeze(validated)
    data[name] = frozen
    mark_dirty(meta)
    meta.session.record(
        "replace",
        path_of(meta) + "/" + escape(name),
        node.adapter.dump_python(frozen, mode="json"),
    )


# --------------------------------------------------------------------------- #
# wrapping                                                                     #
# --------------------------------------------------------------------------- #


def wrappable(value: Any) -> bool:
    return isinstance(value, (BaseModel, list, dict))


def wrap(value: Any, node: AdapterNode, parent: DraftMeta, key: str | int) -> Any:
    """Identity for leaves and existing drafts; a draft node for everything else."""
    cls = type(value)
    if cls is DraftList or cls is DraftDict:
        return value
    if isinstance(value, BaseModel):
        if cls.__dict__.get("__harness_is_draft__", False):
            return value
        return make_model_draft(cast(HarnessState, value), parent.session, parent, key)
    session = parent.session
    if isinstance(value, list):
        return DraftList(
            value,
            DraftMeta(session, parent, key, value),
            node.list_elem or ANY_NODE,
        )
    if isinstance(value, dict):
        return DraftDict(
            value,
            DraftMeta(session, parent, key, value),
            node.dict_key or ANY_NODE,
            node.dict_val or ANY_NODE,
        )
    return value


# --------------------------------------------------------------------------- #
# commit                                                                       #
# --------------------------------------------------------------------------- #


def commit(value: Any) -> Any:
    """Turn a draft tree back into an at-rest value, sharing untouched subtrees."""
    cls = type(value)
    if cls is DraftList:
        meta = value._meta
        if not meta.dirty:
            return meta.original
        return FrozenList(commit(x) for x in list.__iter__(value))
    if cls is DraftDict:
        meta = value._meta
        if not meta.dirty:
            return meta.original
        return FrozenDict((k, commit(v)) for k, v in dict.items(value))
    if isinstance(value, BaseModel) and cls.__dict__.get("__harness_is_draft__", False):
        meta = object.__getattribute__(value, "__pydantic_private__")["_draft"]
        if not meta.dirty:
            return meta.original
        at_rest = cls.__harness_at_rest__
        data = {
            name: commit(item)
            for name, item in object.__getattribute__(value, "__dict__").items()
        }
        if at_rest.__harness_validate_on_commit__:
            rebuilt = at_rest.model_validate(data)
            _restore_untouched(rebuilt, meta.original, data)
            return rebuilt
        # Every value in `data` is already validated and frozen: it either came
        # through an adapter at assignment time or is the original, untouched.
        return at_rest.model_construct(**data)
    return value


def _restore_untouched(rebuilt: Any, original: Any, data: dict[str, Any]) -> None:
    """Put untouched field values back by identity after re-validation.

    Validating a dirty node is shallow, but pydantic-core still builds a fresh
    list/dict/set/tuple for every container field it walks.  Any field whose
    committed value is the *same object* the node was drafted from was not
    touched, so the original is restored and the subtree stays shared.
    """
    rebuilt_dict = rebuilt.__dict__
    original_dict = original.__dict__
    for name, item in data.items():
        if item is original_dict.get(name) and rebuilt_dict.get(name) is not item:
            rebuilt_dict[name] = item


bind_drafts(
    build_draft_class=build_draft_class,
    draft_set_field=draft_set_field,
)

bind(
    wrap=wrap,
    wrappable=wrappable,
    commit=commit,
    mark_dirty=mark_dirty,
    path_of=path_of,
    meta_of=meta_of,
    assert_no_drafts=assert_no_drafts,
    any_node=ANY_NODE,
)
