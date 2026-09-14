# ABOUTME: StateRef — the handle a workflow author holds. The value itself is owned by
# whoever created the ref (the AgentWorkflowRunner, in a workflow), which supplies the
# publisher; the author only ever reads ``ref.current`` and writes inside ``ref.mutate()``.

"""``StateRef``: the handle the developer holds; the harness owns the value."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Callable, Generic, Literal, TypeVar

from pydantic import TypeAdapter

from .base import HarnessState
from .drafts import commit, make_model_draft, meta_of
from .errors import ConcurrentMutationError
from .events import StateEvent, StatePatch, StateSnapshot
from .session import DraftSession

__all__ = ["StateRef"]

T = TypeVar("T", bound=HarnessState)

Publisher = Callable[[StateEvent], None]


def _noop(event: StateEvent) -> None:
    return None


class _MutateContext(AbstractContextManager[T]):
    """The context manager returned by :meth:`StateRef.mutate`."""

    __slots__ = ("_ref", "_session", "_root")

    def __init__(self, ref: "StateRef[T]") -> None:
        self._ref = ref
        self._session: DraftSession | None = None
        self._root: Any = None

    def __enter__(self) -> T:
        ref = self._ref
        if ref._open is not None:
            raise ConcurrentMutationError(
                f"state {ref.state_id!r} already has an open mutate() block; "
                "mutate() is synchronous and must not span an await"
            )
        self._session = DraftSession()
        self._root = make_model_draft(ref._current, self._session, None, None)
        ref._open = self
        return self._root

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        session = self._session
        assert session is not None
        session.alive = False  # revokes every draft node in the tree
        self._ref._open = None
        if exc_type is not None:
            return False  # discard, propagate
        root_meta = meta_of(self._root)
        assert root_meta is not None
        if not root_meta.dirty:
            return False  # no-op: no version bump, no event
        new_value = commit(self._root)  # may raise; current stays untouched
        ref = self._ref
        ref._current = new_value
        ref._version += 1
        ref._emit_patch(session.ops)
        return False


class StateRef(Generic[T]):
    """A named, versioned handle to one immutable state value."""

    __slots__ = ("_state_id", "_current", "_version", "_publish", "_open", "_adapter")

    def __init__(
        self,
        state_id: str,
        value: T,
        publish: Publisher | None = None,
    ) -> None:
        if not isinstance(value, HarnessState):
            raise TypeError(
                f"state {state_id!r}: the root value must be a HarnessState instance, "
                f"got {type(value).__name__}"
            )
        self._state_id = state_id
        self._adapter: TypeAdapter[Any] = TypeAdapter(type(value))
        self._current: T = self._adapter.validate_python(value)
        self._version = 0
        self._publish: Publisher = publish or _noop
        self._open: _MutateContext[T] | None = None

    # -- reads -------------------------------------------------------------- #

    @property
    def current(self) -> T:
        """The latest committed value.  Never a draft."""
        return self._current

    def __call__(self) -> T:
        return self._current

    @property
    def version(self) -> int:
        return self._version

    @property
    def state_id(self) -> str:
        return self._state_id

    def snapshot(self) -> StateSnapshot:
        return StateSnapshot(
            state_id=self._state_id,
            version=self._version,
            value=self._adapter.dump_python(self._current, mode="json"),
        )

    # -- writes ------------------------------------------------------------- #

    def mutate(self) -> AbstractContextManager[T]:
        """Hand out a draft of the current value; commit on clean exit."""
        return _MutateContext(self)

    def set(self, value: T) -> None:
        """Replace the whole root.  Emits one ``replace`` at path ``""``."""
        if self._open is not None:
            raise ConcurrentMutationError(
                f"state {self._state_id!r} has an open mutate() block"
            )
        validated = self._adapter.validate_python(value)
        self._current = validated
        self._version += 1
        self._emit_patch(
            [
                {
                    "op": "replace",
                    "path": "",
                    "value": self._adapter.dump_python(validated, mode="json"),
                }
            ]
        )

    # -- events ------------------------------------------------------------- #

    def _emit_patch(self, ops: list[dict[str, Any]]) -> None:
        self._publish(
            StatePatch(state_id=self._state_id, version=self._version, ops=ops)
        )

    def __repr__(self) -> str:
        return (
            f"StateRef(state_id={self._state_id!r}, version={self._version}, "
            f"type={type(self._current).__name__})"
        )
