# ABOUTME: Observable agent state — deeply immutable Pydantic values plus draft mutation.
# A workflow author declares state by subclassing HarnessState and opts in with one class
# attribute, ``board = agent.state(Board)`` (a StateDecl); from then on every change made inside a
# ``with ref.mutate() as d:`` block is published to the agent's event stream as RFC 6902
# JSON Patch ops. Nothing here imports anything else from ``harness`` — this is a leaf
# package (stdlib + pydantic only) so the runner, the protocol and offline tests can all
# depend on it without a cycle.

"""Observable agent state: immutable values, draft mutation, RFC 6902 patches."""

from __future__ import annotations

from .base import HarnessState
from .errors import (
    ConcurrentMutationError,
    DraftAliasError,
    FrozenError,
    HarnessStateError,
    RevokedDraftError,
    StateSchemaError,
)
from .events import StateEvent, StatePatch, StateSnapshot

# Importing `drafts` wires the draft half of `containers` (see containers.bind).
from . import drafts as _drafts  # noqa: F401
from .ref import StateRef
from .decl import StateDecl, declared_states

__all__ = [
    "HarnessState",
    "StateDecl",
    "StateRef",
    "declared_states",
    "StateSnapshot",
    "StatePatch",
    "StateEvent",
    "HarnessStateError",
    "StateSchemaError",
    "FrozenError",
    "RevokedDraftError",
    "ConcurrentMutationError",
    "DraftAliasError",
]
