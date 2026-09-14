# ABOUTME: The two halves of every list/dict/set in agent state — Frozen* (at rest: every
# mutating method raises FrozenError) and Draft* (inside mutate(): the same stdlib semantics,
# recording one JSON Patch op per mutation). They are real list/dict/set subclasses, so
# isinstance, ==, and pydantic serialization all behave, and the static type stays list[E].

"""Frozen containers (state at rest) and draft containers (state inside ``mutate()``).

The frozen half has no dependency on the draft machinery.  The draft half needs
``_wrap``/``_commit``/``_mark_dirty`` from :mod:`harness.state.drafts`, which in turn
needs the container classes defined here.  The cycle is broken with :func:`bind`,
called once by ``drafts`` at import time.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Mapping, SupportsIndex

from pydantic import BaseModel

from .errors import FrozenError
from .session import MISSING, escape

__all__ = [
    "FrozenList",
    "FrozenDict",
    "FrozenSet",
    "DraftList",
    "DraftDict",
    "DraftSet",
    "freeze",
    "bind",
]


# --------------------------------------------------------------------------- #
# late binding                                                                 #
# --------------------------------------------------------------------------- #


class _Runtime:
    """Slots filled in by :mod:`harness.state.drafts` at import time."""

    wrap: Any = None
    commit: Any = None
    mark_dirty: Any = None
    path_of: Any = None
    meta_of: Any = None
    assert_no_drafts: Any = None
    wrappable: Any = None
    any_node: Any = None


_rt = _Runtime()


def bind(**kwargs: Any) -> None:
    for name, value in kwargs.items():
        setattr(_rt, name, value)


# --------------------------------------------------------------------------- #
# frozen containers                                                            #
# --------------------------------------------------------------------------- #

_LIST_MSG = "list is part of harness state; mutate it inside ref.mutate()"
_DICT_MSG = "dict is part of harness state; mutate it inside ref.mutate()"
_SET_MSG = "set is part of harness state; mutate it inside ref.mutate()"


class FrozenList(list):
    """A ``list`` whose mutating methods raise.  ``isinstance(x, list)`` still holds."""

    __slots__ = ()

    def _blocked(self, *args: Any, **kwargs: Any) -> Any:
        raise FrozenError(_LIST_MSG)

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _blocked
    append = extend = insert = pop = remove = clear = sort = reverse = _blocked

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(tuple(self))

    def __reduce__(self) -> Any:
        # deepcopy/pickle would otherwise rebuild us via append().
        return (type(self), (list(self),))

    def __copy__(self) -> "FrozenList":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenList":
        import copy as _copy

        out = type(self)(_copy.deepcopy(x, memo) for x in list.__iter__(self))
        memo[id(self)] = out
        return out


class FrozenDict(dict):
    """A ``dict`` whose mutating methods raise."""

    __slots__ = ()

    def _blocked(self, *args: Any, **kwargs: Any) -> Any:
        raise FrozenError(_DICT_MSG)

    __setitem__ = __delitem__ = __ior__ = _blocked
    update = pop = popitem = setdefault = clear = _blocked

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(dict.items(self)))

    def __reduce__(self) -> Any:
        return (type(self), (dict(self),))

    def __copy__(self) -> "FrozenDict":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict":
        import copy as _copy

        out = type(self)(
            (_copy.deepcopy(k, memo), _copy.deepcopy(v, memo))
            for k, v in dict.items(self)
        )
        memo[id(self)] = out
        return out


class FrozenSet(set):
    """A ``set`` whose mutating methods raise.

    Iteration is sorted when the elements are orderable so that serialization is
    stable: JSON has no set, and a patch that replaces a set must produce exactly
    the array a snapshot would.
    """

    __slots__ = ()

    def _blocked(self, *args: Any, **kwargs: Any) -> Any:
        raise FrozenError(_SET_MSG)

    add = discard = remove = pop = clear = update = _blocked
    __ior__ = __iand__ = __isub__ = __ixor__ = _blocked
    intersection_update = difference_update = symmetric_difference_update = _blocked

    def __iter__(self) -> Iterator[Any]:
        try:
            return iter(sorted(set.__iter__(self)))
        except TypeError:
            return set.__iter__(self)

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(frozenset(set.__iter__(self)))

    def __reduce__(self) -> Any:
        return (type(self), (list(set.__iter__(self)),))

    def __copy__(self) -> "FrozenSet":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenSet":
        import copy as _copy

        out = type(self)(_copy.deepcopy(x, memo) for x in set.__iter__(self))
        memo[id(self)] = out
        return out


_SCALAR_TYPES = (str, int, float, bool, bytes, type(None))


def freeze(value: Any) -> Any:
    """Return ``value`` with every mutable container replaced by a frozen one.

    Identity for scalars, models (already frozen by their own validator) and
    already-frozen containers, so re-freezing a committed tree is free.
    """
    if type(value) in _SCALAR_TYPES:
        return value
    if isinstance(value, BaseModel):
        return value
    cls = type(value)
    if cls is FrozenList or cls is FrozenDict or cls is FrozenSet:
        return value
    if isinstance(value, list):
        return FrozenList(freeze(x) for x in value)
    if isinstance(value, dict):
        return FrozenDict((k, freeze(v)) for k, v in value.items())
    if isinstance(value, (set, frozenset)):
        # Set members are hashable, hence already immutable.
        return value if isinstance(value, frozenset) else FrozenSet(value)
    if isinstance(value, tuple):
        frozen = tuple(freeze(x) for x in value)
        return frozen
    return value


# --------------------------------------------------------------------------- #
# draft containers                                                             #
# --------------------------------------------------------------------------- #


class DraftList(list):
    """The working list for one list field inside a ``mutate()`` block.

    ``DraftList(orig)`` is an O(n) pointer copy and *is* the working list - there
    is no copy-on-write layer.  Elements start as at-rest values and are replaced
    in place by child draft nodes the first time they are read.
    """

    __slots__ = ("_meta", "_elem")

    def __new__(cls, iterable: Iterable[Any] = (), meta: Any = None, elem: Any = None):
        return list.__new__(cls)

    def __init__(
        self, iterable: Iterable[Any] = (), meta: Any = None, elem: Any = None
    ) -> None:
        list.__init__(self, iterable)
        self._meta = meta
        self._elem = elem if elem is not None else _rt.any_node

    # -- plumbing ---------------------------------------------------------- #

    def _chk(self) -> None:
        self._meta.session.check()

    def _path(self) -> str:
        return _rt.path_of(self._meta)

    def _rec(self, op: str, path: str, value: Any = MISSING) -> None:
        _rt.mark_dirty(self._meta)
        self._meta.session.record(op, path, value)

    def _coerce(self, value: Any) -> Any:
        _rt.assert_no_drafts(value)
        return freeze(self._elem.adapter.validate_python(value))

    def _dump(self, value: Any) -> Any:
        return self._elem.adapter.dump_python(value, mode="json")

    def _dump_all(self) -> list[Any]:
        return [self._dump(x) for x in list.__iter__(self)]

    def _rekey(self, start: int) -> None:
        """Update ``meta.key`` on child drafts after a structural change."""
        for i in range(start, list.__len__(self)):
            meta = _rt.meta_of(list.__getitem__(self, i))
            if meta is not None:
                meta.key = i

    def _index(self, i: SupportsIndex) -> int:
        n = list.__len__(self)
        raw = int(i)
        idx = raw + n if raw < 0 else raw
        if idx < 0 or idx >= n:
            raise IndexError("list index out of range")
        return idx

    # -- reads (never mark dirty) ------------------------------------------ #

    def __getitem__(self, item: Any) -> Any:
        self._chk()
        if isinstance(item, slice):
            return [
                self._read(i) for i in range(*item.indices(list.__len__(self)))
            ]
        return self._read(self._index(item))

    def _read(self, idx: int) -> Any:
        value = list.__getitem__(self, idx)
        if not _rt.wrappable(value):
            return value
        wrapped = _rt.wrap(value, self._elem, self._meta, idx)
        if wrapped is not value:
            list.__setitem__(self, idx, wrapped)
        return wrapped

    def __iter__(self) -> Iterator[Any]:
        self._chk()
        for i in range(list.__len__(self)):
            yield self._read(i)

    # -- writes ------------------------------------------------------------ #

    def append(self, value: Any) -> None:
        self._chk()
        item = self._coerce(value)
        list.append(self, item)
        self._rec("add", self._path() + "/-", self._dump(item))

    def insert(self, index: SupportsIndex, value: Any) -> None:
        self._chk()
        n = list.__len__(self)
        raw = int(index)
        idx = raw + n if raw < 0 else raw
        idx = 0 if idx < 0 else (n if idx > n else idx)
        item = self._coerce(value)
        list.insert(self, idx, item)
        self._rekey(idx)
        self._rec("add", f"{self._path()}/{idx}", self._dump(item))

    def extend(self, iterable: Iterable[Any]) -> None:
        self._chk()
        for value in list(iterable):
            item = self._coerce(value)
            list.append(self, item)
            self._rec("add", self._path() + "/-", self._dump(item))

    def __iadd__(self, iterable: Iterable[Any]) -> "DraftList":  # type: ignore[misc,override]
        self.extend(iterable)
        return self

    def __setitem__(self, item: Any, value: Any) -> None:
        self._chk()
        if isinstance(item, slice):
            items = [self._coerce(v) for v in value]
            list.__setitem__(self, item, items)
            self._rekey(0)
            self._rec("replace", self._path(), self._dump_all())
            return
        idx = self._index(item)
        if value is list.__getitem__(self, idx) and _rt.meta_of(value) is not None:
            return  # in-place operator write-back; the ops are already recorded
        coerced = self._coerce(value)
        list.__setitem__(self, idx, coerced)  # discards any child draft at idx
        self._rec("replace", f"{self._path()}/{idx}", self._dump(coerced))

    def __delitem__(self, item: Any) -> None:
        self._chk()
        if isinstance(item, slice):
            list.__delitem__(self, item)
            self._rekey(0)
            self._rec("replace", self._path(), self._dump_all())
            return
        idx = self._index(item)
        list.__delitem__(self, idx)
        self._rekey(idx)
        self._rec("remove", f"{self._path()}/{idx}")

    def pop(self, index: SupportsIndex = -1) -> Any:
        self._chk()
        idx = self._index(index)
        value = self._read(idx)
        list.__delitem__(self, idx)
        self._rekey(idx)
        self._rec("remove", f"{self._path()}/{idx}")
        return value

    def remove(self, value: Any) -> None:
        self._chk()
        idx = list.index(self, value)
        list.__delitem__(self, idx)
        self._rekey(idx)
        self._rec("remove", f"{self._path()}/{idx}")

    def clear(self) -> None:
        self._chk()
        list.clear(self)
        self._rec("replace", self._path(), [])

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        self._chk()
        list.sort(self, key=key, reverse=reverse)
        self._rekey(0)
        self._rec("replace", self._path(), self._dump_all())

    def reverse(self) -> None:
        self._chk()
        list.reverse(self)
        self._rekey(0)
        self._rec("replace", self._path(), self._dump_all())

    def __imul__(self, count: SupportsIndex) -> "DraftList":  # type: ignore[misc,override]
        self._chk()
        items = list(list.__iter__(self))
        list.clear(self)
        for _ in range(int(count)):
                list.extend(self, items)
        self._rekey(0)
        self._rec("replace", self._path(), self._dump_all())
        return self


class DraftDict(dict):
    """The working dict for one dict field inside a ``mutate()`` block."""

    __slots__ = ("_meta", "_key", "_val")

    def __new__(
        cls,
        mapping: Any = (),
        meta: Any = None,
        key: Any = None,
        val: Any = None,
    ):
        return dict.__new__(cls)

    def __init__(
        self,
        mapping: Any = (),
        meta: Any = None,
        key: Any = None,
        val: Any = None,
    ) -> None:
        dict.__init__(self, mapping)
        self._meta = meta
        self._key = key if key is not None else _rt.any_node
        self._val = val if val is not None else _rt.any_node

    # -- plumbing ---------------------------------------------------------- #

    def _chk(self) -> None:
        self._meta.session.check()

    def _path(self) -> str:
        return _rt.path_of(self._meta)

    def _rec(self, op: str, path: str, value: Any = MISSING) -> None:
        _rt.mark_dirty(self._meta)
        self._meta.session.record(op, path, value)

    def _coerce_key(self, key: Any) -> Any:
        return self._key.adapter.validate_python(key)

    def _coerce(self, value: Any) -> Any:
        _rt.assert_no_drafts(value)
        return freeze(self._val.adapter.validate_python(value))

    def _seg(self, key: Any) -> str:
        # mode="json" is how the key reaches a consumer, so the pointer segment
        # has to be built from the same rendering.
        return escape(str(self._key.adapter.dump_python(key, mode="json")))

    def _dump(self, value: Any) -> Any:
        return self._val.adapter.dump_python(value, mode="json")

    # -- reads ------------------------------------------------------------- #

    def _read(self, key: Any) -> Any:
        value = dict.__getitem__(self, key)
        if not _rt.wrappable(value):
            return value
        wrapped = _rt.wrap(value, self._val, self._meta, self._seg(key))
        if wrapped is not value:
            dict.__setitem__(self, key, wrapped)
        return wrapped

    def __getitem__(self, key: Any) -> Any:
        self._chk()
        if not dict.__contains__(self, key):
            raise KeyError(key)
        return self._read(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._chk()
        if not dict.__contains__(self, key):
            return default
        return self._read(key)

    def values(self) -> Any:
        self._chk()
        return [self._read(k) for k in dict.keys(self)]

    def items(self) -> Any:
        self._chk()
        return [(k, self._read(k)) for k in dict.keys(self)]

    # -- writes ------------------------------------------------------------ #

    def __setitem__(self, key: Any, value: Any) -> None:
        self._chk()
        vkey = self._coerce_key(key)
        if (
            dict.__contains__(self, vkey)
            and value is dict.__getitem__(self, vkey)
            and _rt.meta_of(value) is not None
        ):
            return  # in-place operator write-back; the ops are already recorded
        item = self._coerce(value)
        op = "replace" if dict.__contains__(self, vkey) else "add"
        dict.__setitem__(self, vkey, item)
        self._rec(op, f"{self._path()}/{self._seg(vkey)}", self._dump(item))

    def __delitem__(self, key: Any) -> None:
        self._chk()
        vkey = self._coerce_key(key)
        if not dict.__contains__(self, vkey):
            raise KeyError(key)
        dict.__delitem__(self, vkey)
        self._rec("remove", f"{self._path()}/{self._seg(vkey)}")

    def pop(self, key: Any, *default: Any) -> Any:
        self._chk()
        vkey = self._coerce_key(key)
        if not dict.__contains__(self, vkey):
            if default:
                return default[0]
            raise KeyError(key)
        value = self._read(vkey)
        dict.__delitem__(self, vkey)
        self._rec("remove", f"{self._path()}/{self._seg(vkey)}")
        return value

    def popitem(self) -> tuple[Any, Any]:
        self._chk()
        if not dict.__len__(self):
            raise KeyError("popitem(): dictionary is empty")
        key = next(reversed(dict.keys(self)))
        value = self._read(key)
        dict.__delitem__(self, key)
        self._rec("remove", f"{self._path()}/{self._seg(key)}")
        return (key, value)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._chk()
        vkey = self._coerce_key(key)
        if dict.__contains__(self, vkey):
            return self._read(vkey)
        item = self._coerce(default)
        dict.__setitem__(self, vkey, item)
        self._rec("add", f"{self._path()}/{self._seg(vkey)}", self._dump(item))
        return self._read(vkey)

    def update(self, other: Any = (), /, **kwargs: Any) -> None:
        self._chk()
        if isinstance(other, Mapping):
            pairs = list(other.items())
        else:
            pairs = list(other)
        pairs.extend(kwargs.items())
        for key, value in pairs:
            self[key] = value

    def __ior__(self, other: Any) -> "DraftDict":  # type: ignore[misc,override]
        self.update(other)
        return self

    def clear(self) -> None:
        self._chk()
        dict.clear(self)
        self._rec("replace", self._path(), {})


class DraftSet(set):
    """The working set for one set field inside a ``mutate()`` block.

    JSON has no set and RFC 6902 has no set ops, so every mutation emits one
    ``replace`` carrying the whole serialized set.
    """

    __slots__ = ("_meta", "_elem")

    def __new__(cls, iterable: Iterable[Any] = (), meta: Any = None, elem: Any = None):
        return set.__new__(cls)

    def __init__(
        self, iterable: Iterable[Any] = (), meta: Any = None, elem: Any = None
    ) -> None:
        set.__init__(self, iterable)
        self._meta = meta
        self._elem = elem if elem is not None else _rt.any_node

    def _chk(self) -> None:
        self._meta.session.check()

    def _coerce(self, value: Any) -> Any:
        return self._elem.adapter.validate_python(value)

    def _dump_all(self) -> list[Any]:
        dump = self._elem.adapter.dump_python
        values = [dump(x, mode="json") for x in set.__iter__(self)]
        try:
            values.sort()
        except TypeError:
            pass
        return values

    def _emit(self) -> None:
        _rt.mark_dirty(self._meta)
        self._meta.session.record("replace", _rt.path_of(self._meta), self._dump_all())

    def __iter__(self) -> Iterator[Any]:
        self._chk()
        try:
            return iter(sorted(set.__iter__(self)))
        except TypeError:
            return set.__iter__(self)

    def add(self, value: Any) -> None:
        self._chk()
        item = self._coerce(value)
        if item in self:
            return
        set.add(self, item)
        self._emit()

    def discard(self, value: Any) -> None:
        self._chk()
        item = self._coerce(value)
        if item not in self:
            return
        set.discard(self, item)
        self._emit()

    def remove(self, value: Any) -> None:
        self._chk()
        item = self._coerce(value)
        if item not in self:
            raise KeyError(value)
        set.discard(self, item)
        self._emit()

    def pop(self) -> Any:
        self._chk()
        value = set.pop(self)
        self._emit()
        return value

    def clear(self) -> None:
        self._chk()
        if not set.__len__(self):
            return
        set.clear(self)
        self._emit()

    def _apply(self, op: str, others: tuple[Any, ...]) -> None:
        self._chk()
        sets = [{self._coerce(x) for x in other} for other in others]
        before = set(set.__iter__(self))
        after = set(before)
        for other in sets:
            if op == "update":
                after |= other
            elif op == "intersection_update":
                after &= other
            elif op == "difference_update":
                after -= other
            else:
                after ^= other
        if after == before:
            return
        set.clear(self)
        set.update(self, after)
        self._emit()

    def update(self, *others: Any) -> None:
        self._apply("update", others)

    def intersection_update(self, *others: Any) -> None:
        self._apply("intersection_update", others)

    def difference_update(self, *others: Any) -> None:
        self._apply("difference_update", others)

    def symmetric_difference_update(self, *others: Any) -> None:
        self._apply("symmetric_difference_update", others)

    def __ior__(self, other: Any) -> "DraftSet":  # type: ignore[misc,override]
        self._apply("update", (other,))
        return self

    def __iand__(self, other: Any) -> "DraftSet":  # type: ignore[misc,override]
        self._apply("intersection_update", (other,))
        return self

    def __isub__(self, other: Any) -> "DraftSet":  # type: ignore[misc,override]
        self._apply("difference_update", (other,))
        return self

    def __ixor__(self, other: Any) -> "DraftSet":  # type: ignore[misc,override]
        self._apply("symmetric_difference_update", (other,))
        return self
