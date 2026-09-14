# ABOUTME: Draft nodes — the per-node bookkeeping (DraftMeta), the generated Draft[C]
# subclass of the author's own state class, lazy wrapping of children on first read, and the
# commit that rebuilds an at-rest value while sharing every untouched subtree by identity.

"""Draft nodes: per-node bookkeeping, the generated draft subclass, wrap and commit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from .base import ANY_NODE, AdapterNode, HarnessState
from .containers import (
    DraftDict,
    DraftList,
    DraftSet,
    FrozenDict,
    FrozenList,
    FrozenSet,
    bind,
    freeze,
)
from .errors import DraftAliasError
from .session import DraftSession, escape

__all__ = [
    "DraftMeta",
    "draft_class",
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
    if cls is DraftList or cls is DraftDict or cls is DraftSet:
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


def assert_no_drafts(value: Any) -> None:
    """A draft node has exactly one parent; it may never be stored elsewhere."""
    if meta_of(value) is not None:
        raise DraftAliasError(
            "a draft node cannot be assigned to another location; assign a value "
            "taken from ref.current, or build a new one"
        )
    if isinstance(value, BaseModel):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            assert_no_drafts(item)
    elif isinstance(value, dict):
        for item in value.values():
            assert_no_drafts(item)
    elif isinstance(value, (set, frozenset)):
        for item in value:
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


_DRAFT_CLASSES: dict[type, type] = {}


def draft_class(cls: type[C]) -> type[C]:
    """The generated ``Draft[C]`` subclass of ``C``.  One per user class, cached."""
    generated = _DRAFT_CLASSES.get(cls)
    if generated is None:
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
        _DRAFT_CLASSES[cls] = generated
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
    return isinstance(value, (BaseModel, list, dict, set))


def wrap(value: Any, node: AdapterNode, parent: DraftMeta, key: str | int) -> Any:
    """Identity for leaves and existing drafts; a draft node for everything else."""
    cls = type(value)
    if cls is DraftList or cls is DraftDict or cls is DraftSet:
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
    if isinstance(value, set):  # frozenset is not a set subclass: it stays a leaf
        return DraftSet(
            value,
            DraftMeta(session, parent, key, value),
            node.set_elem or ANY_NODE,
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
    if cls is DraftSet:
        meta = value._meta
        if not meta.dirty:
            return meta.original
        return FrozenSet(set.__iter__(value))
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
