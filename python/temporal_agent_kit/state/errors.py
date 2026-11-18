# ABOUTME: The exception vocabulary for agent state — every one of them names the fix in
# its message, because they all fire at the line that got the contract wrong.

"""Exception types raised by the state layer."""

from __future__ import annotations

__all__ = [
    "AgentStateError",
    "StateSchemaError",
    "FrozenError",
    "RevokedDraftError",
    "ConcurrentMutationError",
    "DraftAliasError",
]


class AgentStateError(Exception):
    """Base class for every error raised by ``harness.state``."""


class StateSchemaError(AgentStateError, TypeError):
    """An ``AgentState`` subclass declares a field the harness cannot own."""


class FrozenError(AgentStateError, TypeError):
    """An at-rest state value (or a container inside one) was mutated."""


class RevokedDraftError(AgentStateError, RuntimeError):
    """A draft node was used after its ``mutate()`` block closed."""


class ConcurrentMutationError(AgentStateError, RuntimeError):
    """A second ``mutate()`` was opened on a ref that already has one open."""


class DraftAliasError(AgentStateError, ValueError):
    """A draft node was assigned into a second location in the tree."""
