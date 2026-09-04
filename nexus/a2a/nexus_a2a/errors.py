"""Errors shared by the Nexus A2A client and authoring APIs."""

from __future__ import annotations

from enum import Enum


class NexusA2AError(RuntimeError):
    """Base error raised by the Nexus A2A binding."""


class NexusA2AOperationError(NexusA2AError):
    """A Nexus operation failed while serving an A2A request."""


class BackendErrorKind(Enum):
    """Protocol-level failure categories a backend can report."""

    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INTERNAL = "internal"


class A2ABackendError(NexusA2AError):
    """A runtime backend rejected an A2A operation."""

    def __init__(self, message: str, *, kind: BackendErrorKind) -> None:
        super().__init__(message)
        self.kind = kind
