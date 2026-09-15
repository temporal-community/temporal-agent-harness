# ABOUTME: A minimal stand-in for AgentWorkflowRunner.state() so the state layer can be
# tested without a workflow. It does exactly what the runner does — construct a StateRef
# with a publisher, then publish the version-0 snapshot — and records what comes out, so
# these tests assert on the same event sequence a real agent puts on its turn_events stream.

from __future__ import annotations

from typing import Any, Callable, TypeVar

from temporal_agent_harness.harness.state import HarnessState, StateRef
from temporal_agent_harness.harness.state.events import StateEvent, StateSnapshot

T = TypeVar("T", bound=HarnessState)


class StateHost:
    """Owns named state refs and forwards their events to a publisher."""

    def __init__(self, publish: Callable[[StateEvent], None] | None = None) -> None:
        self._publish = publish
        self._refs: dict[str, StateRef[Any]] = {}

    def state(self, state_id: str, value: T) -> StateRef[T]:
        if state_id in self._refs:
            raise ValueError(f"state {state_id!r} is already registered")
        ref: StateRef[T] = StateRef(state_id, value, self._emit)
        self._refs[state_id] = ref
        self._emit(ref.snapshot())
        return ref

    def _emit(self, event: StateEvent) -> None:
        if self._publish is not None:
            self._publish(event)


__all__ = ["StateHost", "StateEvent", "StateSnapshot"]
