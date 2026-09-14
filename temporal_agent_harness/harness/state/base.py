# ABOUTME: HarnessState — the immutable-at-rest base model, its definition-time field-type
# checker, and the cached TypeAdapters every mutation validates and serializes through.
# Immutability is enforced at runtime (``__setattr__`` + a container-freezing validator)
# rather than with pydantic's ``frozen=True``, because a draft is typed as this very class
# and ``d.field = ...`` has to typecheck under mypy's pydantic plugin.

"""``HarnessState``: the immutable-at-rest base model, its schema checker and adapters."""

from __future__ import annotations

import collections
import collections.abc as cabc
import dataclasses
import datetime
import decimal
import enum
import ipaddress
import pathlib
import re
import types
import typing
import uuid
import warnings
from typing import Annotated, Any, ClassVar, ForwardRef, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, PrivateAttr, TypeAdapter, model_validator

from .containers import freeze
from .errors import FrozenError, StateSchemaError

__all__ = ["HarnessState", "AdapterNode", "ANY_NODE"]


# --------------------------------------------------------------------------- #
# adapters                                                                     #
# --------------------------------------------------------------------------- #


class AdapterNode:
    """Cached ``TypeAdapter``s for one annotation and the containers inside it.

    One node per ``(model class, field)`` plus element/key/value nodes for
    containers.  Built once at class-definition time; never per mutation.
    """

    __slots__ = ("_ann", "_adapter", "list_elem", "dict_key", "dict_val")

    def __init__(self, annotation: Any) -> None:
        self._ann = annotation
        self._adapter: TypeAdapter[Any] | None = None
        self.list_elem: AdapterNode | None = None
        self.dict_key: AdapterNode | None = None
        self.dict_val: AdapterNode | None = None

    @property
    def adapter(self) -> TypeAdapter[Any]:
        adapter = self._adapter
        if adapter is None:
            adapter = self._adapter = TypeAdapter(self._ann)
        return adapter

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"AdapterNode({self._ann!r})"


ANY_NODE = AdapterNode(Any)


# --------------------------------------------------------------------------- #
# late binding to `drafts`                                                     #
# --------------------------------------------------------------------------- #
#
# `drafts` imports us, so we cannot import it — the same cycle `containers.bind`
# breaks, broken the same way, and for a second reason that is specific to where this
# code runs.  A deferred `from .drafts import ...` *inside a function* is a trap under
# the Temporal sandbox: the sandbox's import hook is active while a workflow module is
# being imported, and `workflow.unsafe.imports_passed_through()` only covers the imports
# written inside it.  An import that fires later, from a hook like
# `__pydantic_init_subclass__`, is not inside anything — the sandbox hands back a
# *sandboxed copy* of `drafts` while the caller already holds the passed-through one.
# Two copies of `DraftList` then exist, `commit()`'s `type(value) is DraftList` says no,
# and a draft container survives into at-rest state, where the next block trips over its
# dead session.  Registering at import time means nothing has to import anything later.


class _Drafts:
    """Functions supplied by :mod:`harness.state.drafts` at import time."""

    build_draft_class: Any = None
    draft_set_field: Any = None


_drafts = _Drafts()


def bind_drafts(**kwargs: Any) -> None:
    for name, value in kwargs.items():
        setattr(_drafts, name, value)


def _strip_annotated(annotation: Any) -> Any:
    while hasattr(annotation, "__metadata__"):
        annotation = get_args(annotation)[0]
    return annotation


def _union_args(annotation: Any) -> tuple[Any, ...] | None:
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return get_args(annotation)
    return None


def _build_node(annotation: Any) -> AdapterNode:
    node = AdapterNode(annotation)
    _fill_node(node, annotation)
    try:
        node.adapter  # eager: schema compilation must never land on a mutation
    except Exception:  # pragma: no cover - deferred until the forward ref resolves
        node._adapter = None
    return node


def _fill_node(node: AdapterNode, annotation: Any) -> None:
    bare = _strip_annotated(annotation)
    members = _union_args(bare)
    if members is not None:
        for member in members:
            _fill_node(node, member)
        return
    origin = get_origin(bare)
    args = get_args(bare)
    if origin is list and args and node.list_elem is None:
        node.list_elem = _build_node(args[0])
    elif origin is dict and len(args) == 2 and node.dict_key is None:
        node.dict_key = _build_node(args[0])
        node.dict_val = _build_node(args[1])


# Config keys that cannot change how an already-validated value validates, so a
# class that sets only these can be rebuilt at commit without re-validation.
_INERT_CONFIG_KEYS = frozenset(
    {
        "arbitrary_types_allowed",
        "defer_build",
        "extra",
        "field_title_generator",
        "frozen",
        "harness_validate_on_commit",
        "json_schema_extra",
        "model_title_generator",
        "protected_namespaces",
        "revalidate_instances",
        "ser_json_bytes",
        "ser_json_inf_nan",
        "ser_json_timedelta",
        "title",
        "use_attribute_docstrings",
    }
)


def _needs_commit_validation(cls: type["HarnessState"]) -> bool:
    """Whether committing a dirty node of ``cls`` must go through ``model_validate``.

    Every assignment and every container insertion is already validated against
    the field's own annotation, so re-validating a whole node at commit only
    buys back the hooks a stand-alone ``TypeAdapter`` cannot see: the class's
    ``field_validator`` / ``model_validator`` functions and any config key that
    changes how values validate.  When a class declares none of those, commit
    rebuilds the node with ``model_construct`` instead, which is what keeps a
    commit proportional to what was touched rather than to the size of the node
    (design §13; the benchmark is ~475x on a 10k-element field).

    ``model_config["harness_validate_on_commit"]`` overrides the decision either way.
    """
    explicit = cls.model_config.get("harness_validate_on_commit")
    if explicit is not None:
        return bool(explicit)
    decorators = cls.__pydantic_decorators__
    if decorators.field_validators or decorators.root_validators or decorators.validators:
        return True
    if any(name != "_freeze_containers" for name in decorators.model_validators):
        return True
    return any(key not in _INERT_CONFIG_KEYS for key in cls.model_config)


def _precompute_adapters(cls: type["HarnessState"]) -> None:
    adapters: dict[str, AdapterNode] = {}
    for name, field in cls.model_fields.items():
        annotation: Any = field.annotation
        if field.metadata:
            annotation = Annotated[tuple([annotation, *field.metadata])]
        adapters[name] = _build_node(annotation)
    cls.__harness_adapters__ = adapters
    cls.__harness_field_names__ = frozenset(cls.model_fields)


# --------------------------------------------------------------------------- #
# schema checker                                                               #
# --------------------------------------------------------------------------- #

_ALLOWED_SCALARS: frozenset[Any] = frozenset(
    {
        str,
        int,
        float,
        bool,
        bytes,
        complex,
        type(None),
        decimal.Decimal,
        datetime.datetime,
        datetime.date,
        datetime.time,
        datetime.timedelta,
        uuid.UUID,
        pathlib.PurePath,
        pathlib.PurePosixPath,
        pathlib.PureWindowsPath,
        pathlib.Path,
        ipaddress.IPv4Address,
        ipaddress.IPv6Address,
        ipaddress.IPv4Network,
        ipaddress.IPv6Network,
        re.Pattern,
    }
)

_ABSTRACT_REJECTS: dict[Any, str] = {
    cabc.Sequence: "list[...]",
    cabc.MutableSequence: "list[...]",
    cabc.Mapping: "dict[..., ...]",
    cabc.MutableMapping: "dict[..., ...]",
    cabc.Set: "list[...]",
    cabc.MutableSet: "list[...]",
    cabc.Iterable: "list[...] or tuple[...]",
    cabc.Iterator: "list[...]",
    cabc.Collection: "list[...]",
    cabc.Container: "list[...]",
    collections.deque: "list[...]",
    collections.OrderedDict: "dict[..., ...]",
    collections.defaultdict: "dict[..., ...]",
    collections.Counter: "dict[..., int]",
}


def _fail(cls: type, field: str, detail: str, fix: str) -> None:
    raise StateSchemaError(f"{cls.__name__}.{field}: {detail}  Use {fix} instead.")


def _reject_set(cls: type, field: str, name: str) -> None:
    """Sets are not state the harness can own, and the reason is the whole feature.

    JSON has no set and RFC 6902 has no set operation, so a set can only ever be published by
    re-sending the whole collection — the coarsest op in a layer whose entire point is that a
    change costs what it touched. Worse, keeping that array stable across workers meant sorting
    on every read, which is only possible when the elements are orderable; `set[str | None]`
    fell back to CPython's hash order, and that varies per process, so two workers replaying one
    history published different bytes.

    A `list` says the same thing honestly: the author owns the ordering and the de-duplication,
    and in exchange an addition is `add /tags/-` rather than a re-send of every tag.
    """
    raise StateSchemaError(
        f"{cls.__name__}.{field}: `{name}` is not supported as state. JSON has no set and "
        "RFC 6902 has no set operation, so every change would re-send the whole collection, "
        "and its order could not be made stable across workers. Use list[...] instead and "
        "de-duplicate at the point you append."
    )


_AMBIGUOUS_KEY_REASON = (
    "A snapshot is a JSON object, and JSON object keys are strings, so two keys that "
    'render the same - `1` and `"1"`, or `date(2020, 1, 2)` and `"2020-01-02"` - are '
    "one key there while Python holds two. The snapshot silently loses one of them, and "
    "a patch path can only ever name the survivor, so a consumer applying the patch "
    "changes the wrong entry."
)


def _check_dict_key(cls: type, field: str, annotation: Any) -> None:
    """A dict key has to survive being written down as a JSON object key.

    This is the one part of the layer that genuinely cannot be fixed at the pointer
    level, which is why it is a schema rule rather than a pointer rule: the snapshot
    *is* a JSON object, its keys *are* strings, and no pointer scheme can address an
    entry that the document does not contain.  Any disambiguating scheme would describe
    a document `model_dump` does not produce.

    So the rule is a positive one - a scalar, an ``Enum``, or a ``Literal`` of those,
    each of which has one faithful spelling as an object key - and everything else is
    refused at class definition with the reason attached.
    """
    annotation = _strip_annotated(annotation)

    if annotation is Any or isinstance(annotation, (ForwardRef, str)):
        _check_annotation(cls, field, annotation, set())  # `Any` warns; a ref defers
        return

    if _union_args(annotation) is not None:
        raise StateSchemaError(
            f"{cls.__name__}.{field}: `{annotation}` is not supported as a dict KEY. "
            f"{_AMBIGUOUS_KEY_REASON} Use a single key type, and put the variation in "
            "the value if you need it."
        )

    if get_origin(annotation) is Literal:
        values = get_args(annotation)
        _check_annotation(cls, field, annotation, set())  # the members must be scalars
        if len(_json_key_renderings(annotation, values)) < len(set(values)):
            raise StateSchemaError(
                f"{cls.__name__}.{field}: `{annotation}` is not supported as a dict "
                f"KEY - two of its members render as the same JSON object key. "
                f"{_AMBIGUOUS_KEY_REASON} Use members that stay distinct as strings."
            )
        return

    if isinstance(annotation, type) and (
        annotation in _ALLOWED_SCALARS or issubclass(annotation, enum.Enum)
    ):
        return

    _fail(
        cls,
        field,
        f"`{getattr(annotation, '__name__', annotation)}` is not a supported dict KEY. "
        "A JSON object key is a string, and there is no faithful string spelling of "
        "this one - a model key reaches the snapshot as its repr and the pointer as "
        "something else again, so the two never agree.",
        "a scalar, an Enum, or a Literal of those, as the key",
    )


def _json_key_renderings(annotation: Any, values: tuple[Any, ...]) -> dict[str, Any]:
    """How pydantic would write ``values`` as the object keys of ``dict[annotation, ...]``."""
    try:
        return TypeAdapter(dict[annotation, int]).dump_python(  # type: ignore[valid-type]
            {value: 0 for value in values}, mode="json"
        )
    except Exception:  # pragma: no cover - never fail a class definition over this
        return {str(value): 0 for value in values}


def _check_annotation(cls: type, field: str, annotation: Any, seen: set[int]) -> None:
    annotation = _strip_annotated(annotation)

    if annotation is Any:
        warnings.warn(
            f"{cls.__name__}.{field}: `Any` is an escape hatch - a mutable object "
            "behind it can be changed in place without producing a patch.",
            stacklevel=4,
        )
        return
    if isinstance(annotation, (ForwardRef, str)):
        return  # unresolved; pydantic rebuilds later

    members = _union_args(annotation)
    if members is not None:
        for member in members:
            _check_annotation(cls, field, member, seen)
        return

    origin = get_origin(annotation)

    if origin is Literal:
        for value in get_args(annotation):
            if value is not None and not isinstance(
                value, (str, int, float, bool, bytes, enum.Enum)
            ):
                _fail(cls, field, f"Literal value {value!r} is not immutable.", "a scalar Literal")
        return

    if origin is not None:
        if origin in _ABSTRACT_REJECTS:
            _fail(
                cls,
                field,
                f"`{getattr(origin, '__name__', origin)}` is not a concrete "
                "container the harness can own.",
                _ABSTRACT_REJECTS[origin],
            )
        args = get_args(annotation)
        if origin is list:
            if len(args) != 1:
                _fail(cls, field, "`list` must be parameterized.", "list[E]")
            _check_annotation(cls, field, args[0], seen)
            return
        if origin is dict:
            if len(args) != 2:
                _fail(cls, field, "`dict` must be parameterized.", "dict[K, V]")
            _check_dict_key(cls, field, args[0])
            _check_annotation(cls, field, args[1], seen)
            return
        if origin is set or origin is frozenset:
            _reject_set(cls, field, origin.__name__)
        if origin is tuple:
            for arg in args:
                if arg is Ellipsis:
                    continue
                _check_annotation(cls, field, arg, seen)
            return
        _fail(
            cls,
            field,
            f"`{getattr(origin, '__name__', origin)}[...]` is not a supported "
            "state type.",
            "list / dict / tuple, a HarnessState subclass, or a scalar",
        )

    if annotation in _ALLOWED_SCALARS:
        return
    if isinstance(annotation, type):
        if annotation in _ABSTRACT_REJECTS:
            _fail(
                cls,
                field,
                f"`{annotation.__name__}` is not a concrete container the harness can own.",
                _ABSTRACT_REJECTS[annotation],
            )
        if issubclass(annotation, enum.Enum):
            return
        if issubclass(annotation, HarnessState):
            return
        if issubclass(annotation, BaseModel):
            _fail(
                cls,
                field,
                f"`{annotation.__name__}` is a plain BaseModel, which the harness "
                "cannot freeze or draft.",
                "a HarnessState subclass",
            )
        if dataclasses.is_dataclass(annotation):
            _fail(
                cls,
                field,
                f"`{annotation.__name__}` is a dataclass.",
                "a HarnessState subclass",
            )
        if issubclass(annotation, dict) and hasattr(annotation, "__annotations__") and hasattr(
            annotation, "__required_keys__"
        ):
            _fail(
                cls, field, f"`{annotation.__name__}` is a TypedDict.", "a HarnessState subclass"
            )
        if annotation in (list, dict, tuple):
            _fail(
                cls,
                field,
                f"bare `{annotation.__name__}` leaves the element type unknown.",
                f"{annotation.__name__}[...]",
            )
        if issubclass(annotation, (set, frozenset)):
            _reject_set(cls, field, annotation.__name__)
        if issubclass(annotation, (list, dict)):
            _fail(
                cls,
                field,
                f"`{annotation.__name__}` is a container subclass the harness "
                "cannot rebuild.",
                "list[...] / dict[...]",
            )
        _fail(
            cls,
            field,
            f"`{annotation.__name__}` is not known to be immutable.",
            "a scalar, an Enum, a HarnessState subclass, or a supported container",
        )
    _fail(
        cls,
        field,
        f"`{annotation!r}` is not a supported state annotation.",
        "a scalar, an Enum, a HarnessState subclass, or a supported container",
    )


def _check_field_types(cls: type["HarnessState"]) -> None:
    private = {
        name for name in getattr(cls, "__private_attributes__", {}) if name != "_draft"
    }
    if private:
        raise StateSchemaError(
            f"{cls.__name__}: private attributes {sorted(private)} are not allowed - "
            "they are reinitialized on re-validation and never appear in a patch."
        )
    computed = getattr(cls, "model_computed_fields", None) or {}
    if computed:
        raise StateSchemaError(
            f"{cls.__name__}: computed fields {sorted(computed)} are not allowed - "
            "they would appear in a snapshot but never in a patch."
        )
    for name, field in cls.model_fields.items():
        _check_annotation(cls, name, field.annotation, set())


# --------------------------------------------------------------------------- #
# the base model                                                               #
# --------------------------------------------------------------------------- #


class HarnessState(BaseModel):
    """Deeply immutable at rest; mutable only as a draft inside ``ref.mutate()``.

    ``frozen=True`` is deliberately not used: mypy's pydantic plugin would reject
    ``d.field = ...`` on a draft, and the draft is typed as this very class.
    """

    model_config = ConfigDict(
        extra="forbid",
        revalidate_instances="never",
        arbitrary_types_allowed=False,
    )

    _draft: Any = PrivateAttr(default=None)

    __harness_adapters__: ClassVar[dict[str, AdapterNode]] = {}
    __harness_field_names__: ClassVar[frozenset[str]] = frozenset()
    __harness_is_draft__: ClassVar[bool] = False
    __harness_validate_on_commit__: ClassVar[bool] = True
    # Set on each subclass by `__pydantic_init_subclass__`; read only through
    # `cls.__dict__`, never inherited (see `drafts.draft_class`).
    __harness_draft_class__: ClassVar[type | None] = None

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            return super().__setattr__(name, value)
        private = self.__pydantic_private__
        meta = private.get("_draft") if private else None
        if meta is None:
            raise FrozenError(
                f"{type(self).__name__}.{name}: state is immutable at rest; "
                "mutate it inside ref.mutate()"
            )
        _drafts.draft_set_field(self, meta, name, value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_"):
            return super().__delattr__(name)
        raise FrozenError(
            f"{type(self).__name__}.{name}: state fields cannot be deleted"
        )

    @model_validator(mode="after")
    def _freeze_containers(self) -> "HarnessState":
        data = self.__dict__
        for name in type(self).model_fields:
            value = data.get(name)
            frozen = freeze(value)
            if frozen is not value:
                data[name] = frozen
        return self

    def __hash__(self) -> int:
        return hash((type(self), tuple(self.__dict__.values())))

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if cls.__dict__.get("__harness_is_draft__", False):
            return  # generated draft subclasses inherit everything already checked
        _check_field_types(cls)
        _precompute_adapters(cls)
        cls.__harness_validate_on_commit__ = _needs_commit_validation(cls)

        # Build `Draft[cls]` now, not on the first `mutate()`.  Definition time is where
        # a generated class belongs: it is bounded by the number of declared state
        # classes rather than by traffic, it keeps the ~100-250 us of class creation off
        # the workflow task, and because class creation is already serialized behind the
        # metaclass it removes the read-modify-write race two workflow threads could hit
        # drafting the same class at once.  `None` only while `drafts` is still being
        # imported, which no user class can be defined during; `draft_class` builds it on
        # demand for anything that slips through.
        if _drafts.build_draft_class is not None:
            _drafts.build_draft_class(cls)
