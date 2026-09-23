# ABOUTME: StateDecl — the class-level declaration of one piece of observable agent state.
# An agent writes ``board = agent.state(Board)`` in its class body; the attribute name is the
# state id, the class reads back the declaration (what static tooling like codegen reads), and
# each workflow instance reads back its own StateRef. The AgentWorkflowRunner finds these
# declarations on the agent's class and publishes them; nothing else can create published state.

"""``StateDecl``: observable state declared on the agent class."""

from __future__ import annotations

from typing import Any, Callable, Generic, TypeVar, overload

from .base import HarnessState
from .ref import StateRef

__all__ = ["StateDecl", "declared_states", "state", "state_ref"]

T = TypeVar("T", bound=HarnessState)

# Where an instance keeps its refs, keyed by state id. On the instance, never on the
# descriptor: the descriptor is a class attribute shared by every workflow instance on a
# worker, so a ref held there would leak one workflow's state into another.
_REFS_ATTR = "__harness_state_refs__"


class StateDecl(Generic[T]):
    """One piece of observable state, declared as a class attribute of an agent.

    Read from the class, it is this declaration; read from an instance, it is that
    instance's :class:`StateRef`. The ref is created on first access with the declared
    initial value and publishes nothing until the agent's runner attaches it.
    """

    __slots__ = ("state_type", "_initial", "_state_id")

    def __init__(
        self, state_type: type[T], *, initial: Callable[[], T] | None = None
    ) -> None:
        if not (isinstance(state_type, type) and issubclass(state_type, HarnessState)):
            raise TypeError(
                f"agent.state({state_type!r}): the state type must be a HarnessState "
                "subclass"
            )
        if initial is None:
            required = [n for n, f in state_type.model_fields.items() if f.is_required()]
            if required:
                raise TypeError(
                    f"agent.state({state_type.__name__}): {state_type.__name__} has required "
                    f"fields {required}, so it has no default value to start from; pass "
                    f"initial=lambda: {state_type.__name__}(...)"
                )
        self.state_type = state_type
        self._initial = initial
        self._state_id: str | None = None

    def __set_name__(self, owner: type, name: str) -> None:
        if self._state_id is not None and self._state_id != name:
            raise TypeError(
                f"{owner.__name__}.{name}: this agent.state(...) is already declared as "
                f"{self._state_id!r}; each state needs its own agent.state(...)"
            )
        self._state_id = name

    @property
    def state_id(self) -> str:
        """The id the state is published under: the attribute name it was declared as."""
        if self._state_id is None:
            raise TypeError(
                f"agent.state({self.state_type.__name__}) is not a class attribute; declare "
                "it in the agent's class body, e.g. `board = agent.state(Board)`"
            )
        return self._state_id

    def initial_value(self) -> T:
        value = self._initial() if self._initial is not None else self.state_type()
        if not isinstance(value, self.state_type):
            raise TypeError(
                f"state {self.state_id!r}: initial() returned {type(value).__name__}, not "
                f"{self.state_type.__name__}"
            )
        return value

    @overload
    def __get__(self, obj: None, owner: type | None = None) -> StateDecl[T]: ...
    @overload
    def __get__(self, obj: object, owner: type | None = None) -> StateRef[T]: ...
    def __get__(self, obj: object | None, owner: type | None = None) -> Any:
        if obj is None:
            return self
        return state_ref(obj, self)

    def __set__(self, obj: object, value: object) -> None:
        # A data descriptor, so `self.board = Board()` cannot silently shadow the declared
        # state with an unpublished attribute.
        raise AttributeError(
            f"{self.state_id!r} is declared state and cannot be reassigned; change it with "
            f"`with self.{self.state_id}.mutate() as d:` or replace it with "
            f"`self.{self.state_id}.set(...)`"
        )

    def __repr__(self) -> str:
        return f"StateDecl({self._state_id!r}, {self.state_type.__name__})"


def state(state_type: type[T], *, initial: Callable[[], T] | None = None) -> StateDecl[T]:
    """Declare observable state on an agent class; the attribute name is its state id::

        class MyAgent:
            plan = agent.state(PlanState)
            board = agent.state(Board, initial=lambda: Board(size=9))

    ``initial`` builds the starting value; omit it when the type's defaults are the start.
    Every committed ``with self.plan.mutate() as d:`` is then published to the agent's event
    stream as JSON Patch ops, after one snapshot when the runner is constructed.
    """
    return StateDecl(state_type, initial=initial)


def state_ref(obj: object, decl: StateDecl[T]) -> StateRef[T]:
    """``obj``'s ref for ``decl``, created with the declared initial value on first use."""
    try:
        refs: dict[str, StateRef[Any]] = vars(obj).setdefault(_REFS_ATTR, {})
    except TypeError:
        raise TypeError(
            f"{type(obj).__name__} declares state but its instances have no __dict__ "
            "(__slots__); declared state is stored on the instance"
        ) from None
    ref = refs.get(decl.state_id)
    if ref is None:
        ref = StateRef(decl.state_id, decl.initial_value())
        refs[decl.state_id] = ref
    return ref


def declared_states(cls: type) -> dict[str, StateDecl[Any]]:
    """Every state declared on ``cls`` and its bases, keyed by id, in declaration order.

    Pure reflection over the class. A subclass attribute of the same name overrides its
    base's, including with something that is not a declaration.
    """
    found: dict[str, StateDecl[Any]] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            if isinstance(value, StateDecl):
                found[name] = value
            else:
                found.pop(name, None)
    return found
