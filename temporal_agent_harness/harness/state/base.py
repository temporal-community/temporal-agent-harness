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

    __slots__ = ("_ann", "_adapter", "list_elem", "dict_key", "dict_val", "set_elem")

    def __init__(self, annotation: Any) -> None:
        self._ann = annotation
        self._adapter: TypeAdapter[Any] | None = None
        self.list_elem: AdapterNode | None = None
        self.dict_key: AdapterNode | None = None
        self.dict_val: AdapterNode | None = None
        self.set_elem: AdapterNode | None = None

    @property
    def adapter(self) -> TypeAdapter[Any]:
        adapter = self._adapter
        if adapter is None:
            adapter = self._adapter = TypeAdapter(self._ann)
        return adapter

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"AdapterNode({self._ann!r})"


ANY_NODE = AdapterNode(Any)


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
    elif origin is set and args and node.set_elem is None:
        node.set_elem = _build_node(args[0])


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
    cabc.Set: "set[...] or frozenset[...]",
    cabc.MutableSet: "set[...]",
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
            _check_annotation(cls, field, args[0], seen)
            _check_annotation(cls, field, args[1], seen)
            return
        if origin is set or origin is frozenset:
            if len(args) != 1:
                _fail(cls, field, f"`{origin.__name__}` must be parameterized.", f"{origin.__name__}[E]")
            _check_annotation(cls, field, args[0], seen)
            return
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
            "list / dict / set / frozenset / tuple, a HarnessState subclass, or a scalar",
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
        if annotation in (list, dict, set, frozenset, tuple):
            _fail(
                cls,
                field,
                f"bare `{annotation.__name__}` leaves the element type unknown.",
                f"{annotation.__name__}[...]",
            )
        if issubclass(annotation, (list, dict, set)):
            _fail(
                cls,
                field,
                f"`{annotation.__name__}` is a container subclass the harness "
                "cannot rebuild.",
                "list[...] / dict[...] / set[...]",
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
        from .drafts import draft_set_field

        draft_set_field(self, meta, name, value)

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
