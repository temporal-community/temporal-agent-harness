"""Durable workflow-side protocol for operations fulfilled by a local bridge.

``LocalOperations`` deliberately has no dependency on ``AgentWorkflowRunner``. Any Temporal
workflow can own an instance, expose :meth:`LocalOperations.pending` as a query, and forward a
validated update to :meth:`LocalOperations.complete`. The bridge may be a browser, native
process, or any other executor that can address the workflow.

Callers supply operation ids and idempotency keys. This keeps registration deterministic on
workflow replay and gives an executor a stable key with which to suppress repeated side effects.
An idempotency key identifies the same semantic operation across every run that reuses a Temporal
workflow ID. Callers must therefore choose a new key when a new run intends a new side effect.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar, cast

from pydantic import TypeAdapter, ValidationError
from temporalio import workflow


PENDING_LOCAL_OPERATIONS_QUERY = "pending_local_operations"
COMPLETE_LOCAL_OPERATION_UPDATE = "complete_local_operation"


class LocalOperationStatus(StrEnum):
    """Lifecycle state of a bridge-fulfilled operation."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class LocalOperationRequest:
    """A typed, routable operation exposed to a local bridge.

    ``bridge_id`` selects the paired executor and ``root_id`` selects one of that bridge's
    user-authorized roots. ``arguments`` must contain only operation-specific data; routing
    context is explicit so a server can authorize it before forwarding a completion update.

    ``idempotency_key`` is scoped by the owning Temporal workflow ID, not its run ID. It must
    remain globally unique for a semantic operation across workflow-ID reuse so a bridge can
    safely retain executed outcomes across process and workflow restarts.

    ``output_schema`` is filled by :meth:`LocalOperations.start` from ``result_type``. Callers
    normally leave it empty.
    """

    operation_id: str
    bridge_id: str
    root_id: str
    kind: str
    arguments: dict[str, Any]
    idempotency_key: str
    output_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LocalOperationResult:
    """Completion submitted by a bridge.

    ``error is None`` denotes success, including a successful ``None`` result. A non-``None``
    error denotes bridge failure and causes the waiter to raise :class:`LocalOperationFailed`.
    """

    operation_id: str
    result: Any = None
    error: str | None = None


@dataclass(frozen=True)
class LocalOperationOutcome:
    """Current or terminal state of an operation."""

    operation_id: str
    status: LocalOperationStatus
    result: Any = None
    error: str | None = None


@dataclass(frozen=True)
class LocalOperationAck:
    """Acknowledgement returned after a completion is recorded."""

    operation_id: str
    accepted: bool = True


class LocalOperationProtocolError(ValueError):
    """Base class for deterministic request/completion contract errors."""


class InvalidLocalOperationRequest(LocalOperationProtocolError):
    """The workflow attempted to register an incomplete request."""


class DuplicateLocalOperation(LocalOperationProtocolError):
    """An operation id or idempotency key conflicts with an existing request."""


class UnknownLocalOperation(LocalOperationProtocolError):
    """No registered operation has the supplied id."""


class LocalOperationAlreadyResolved(LocalOperationProtocolError):
    """A completion targeted an operation that is no longer pending."""


class MalformedLocalOperationResult(LocalOperationProtocolError):
    """A successful completion did not match the declared result type."""

    def __init__(self, operation_id: str, validation_error: ValidationError) -> None:
        self.operation_id = operation_id
        self.validation_error = validation_error
        super().__init__(
            f"local operation result for operation_id={operation_id!r} does not match "
            f"the declared output type. Validation error: {validation_error}"
        )


class LocalOperationFailed(Exception):
    """A pending operation ended without a successful result."""

    def __init__(self, outcome: LocalOperationOutcome) -> None:
        self.outcome = outcome
        detail = f": {outcome.error}" if outcome.error else ""
        super().__init__(
            f"local operation {outcome.operation_id!r} {outcome.status.value}{detail}"
        )


class _WaitCondition(Protocol):
    def __call__(
        self,
        condition: Callable[[], bool],
        *,
        timeout: timedelta | None = None,
    ) -> Awaitable[None]: ...


ResultT = TypeVar("ResultT")


@dataclass
class _Entry(Generic[ResultT]):
    request: LocalOperationRequest
    output_adapter: TypeAdapter[ResultT]
    status: LocalOperationStatus = LocalOperationStatus.PENDING
    result: ResultT | None = None
    error: str | None = None

    def outcome(self) -> LocalOperationOutcome:
        return LocalOperationOutcome(
            operation_id=self.request.operation_id,
            status=self.status,
            result=self.result,
            error=self.error,
        )


class LocalOperations:
    """Deterministic workflow state for bridge-fulfilled local operations.

    A containing workflow typically wires the public protocol like this::

        @workflow.query(name=PENDING_LOCAL_OPERATIONS_QUERY)
        def pending_local_operations(self) -> list[LocalOperationRequest]:
            return self.local_operations.pending()

        @workflow.update(name=COMPLETE_LOCAL_OPERATION_UPDATE)
        async def complete_local_operation(self, result: LocalOperationResult):
            return self.local_operations.complete(result)

        @complete_local_operation.validator
        def validate_complete_local_operation(self, result: LocalOperationResult):
            self.local_operations.validate_completion(result)

    Registration, validation, and completion are synchronous state transitions. Only
    :meth:`await_result` waits, using Temporal's deterministic ``workflow.wait_condition``.
    ``wait_condition`` is injectable solely to make the state machine unit-testable without a
    Temporal test server.
    """

    def __init__(self, *, wait_condition: _WaitCondition | None = None) -> None:
        self._entries: dict[str, _Entry[Any]] = {}
        self._idempotency_keys: dict[str, str] = {}
        self._wait_condition = wait_condition or cast(
            _WaitCondition, workflow.wait_condition
        )

    def start(
        self, request: LocalOperationRequest, result_type: Any
    ) -> LocalOperationRequest:
        """Register ``request`` and return its canonical query representation.

        Re-registering the exact same operation is idempotent. Reusing an operation id for a
        different request, or an idempotency key for a different operation, is rejected before
        any state changes.
        """
        self._validate_request(request)
        adapter: TypeAdapter[Any] = (
            result_type
            if isinstance(result_type, TypeAdapter)
            else TypeAdapter(result_type)
        )
        canonical = replace(request, output_schema=adapter.json_schema())

        existing = self._entries.get(request.operation_id)
        if existing is not None:
            if existing.request == canonical:
                return existing.request
            raise DuplicateLocalOperation(
                f"operation_id={request.operation_id!r} is already registered with a "
                "different request"
            )

        owner = self._idempotency_keys.get(request.idempotency_key)
        if owner is not None:
            raise DuplicateLocalOperation(
                f"idempotency_key={request.idempotency_key!r} is already used by "
                f"operation_id={owner!r}"
            )

        self._entries[request.operation_id] = _Entry(
            request=canonical, output_adapter=adapter
        )
        self._idempotency_keys[request.idempotency_key] = request.operation_id
        return canonical

    async def run(
        self,
        request: LocalOperationRequest,
        result_type: Any,
        *,
        timeout: timedelta | None = None,
    ) -> Any:
        """Register an operation and wait for its validated result."""
        canonical = self.start(request, result_type)
        return await self.await_result(canonical.operation_id, timeout=timeout)

    def pending(self) -> list[LocalOperationRequest]:
        """Return pending requests in deterministic registration order."""
        return [
            entry.request
            for entry in self._entries.values()
            if entry.status is LocalOperationStatus.PENDING
        ]

    def status(self, operation_id: str) -> LocalOperationOutcome:
        """Return the current state for ``operation_id``."""
        return self._entry(operation_id).outcome()

    def is_resolved(self, operation_id: str) -> bool:
        """Whether a known operation has reached a terminal state."""
        entry = self._entries.get(operation_id)
        return entry is not None and entry.status is not LocalOperationStatus.PENDING

    def validate_completion(self, result: LocalOperationResult) -> None:
        """Validate an update without consuming its pending operation.

        This method is mutation-free and suitable for a Temporal update validator. A malformed
        successful payload leaves the operation pending, so a bridge can correct and resubmit it.
        """
        entry = self._pending_entry(result.operation_id)
        if result.error is not None:
            return
        try:
            entry.output_adapter.validate_python(result.result)
        except ValidationError as error:
            raise MalformedLocalOperationResult(result.operation_id, error) from error

    def complete(self, result: LocalOperationResult) -> LocalOperationAck:
        """Validate, coerce, and record one bridge completion."""
        self.validate_completion(result)
        entry = self._pending_entry(result.operation_id)
        if result.error is not None:
            entry.status = LocalOperationStatus.FAILED
            entry.result = None
            entry.error = result.error
        else:
            entry.status = LocalOperationStatus.SUCCEEDED
            entry.result = entry.output_adapter.validate_python(result.result)
            entry.error = None
        return LocalOperationAck(operation_id=result.operation_id)

    def cancel(self, operation_id: str, *, reason: str = "operation cancelled") -> None:
        """Cancel one pending operation, waking its waiter with a failure."""
        entry = self._pending_entry(operation_id)
        entry.status = LocalOperationStatus.CANCELLED
        entry.result = None
        entry.error = reason

    def cancel_pending(self, *, reason: str = "workflow closed") -> list[str]:
        """Cancel all pending operations and return their ids in registration order."""
        cancelled: list[str] = []
        for entry in self._entries.values():
            if entry.status is not LocalOperationStatus.PENDING:
                continue
            entry.status = LocalOperationStatus.CANCELLED
            entry.result = None
            entry.error = reason
            cancelled.append(entry.request.operation_id)
        return cancelled

    async def await_result(
        self, operation_id: str, *, timeout: timedelta | None = None
    ) -> Any:
        """Wait without consuming an activity timeout, returning the typed result.

        A deadline transitions a still-pending entry to ``timed_out``. Bridge failures,
        cancellation, and timeouts raise :class:`LocalOperationFailed` with the terminal outcome.
        """
        entry = self._entry(operation_id)
        if entry.status is LocalOperationStatus.PENDING:
            try:
                await self._wait_condition(
                    lambda: entry.status is not LocalOperationStatus.PENDING,
                    timeout=timeout,
                )
            except TimeoutError:
                if entry.status is LocalOperationStatus.PENDING:
                    entry.status = LocalOperationStatus.TIMED_OUT
                    entry.result = None
                    entry.error = "operation timed out before a result arrived"

        outcome = entry.outcome()
        if outcome.status is LocalOperationStatus.SUCCEEDED:
            return entry.result
        raise LocalOperationFailed(outcome)

    @staticmethod
    def _validate_request(request: LocalOperationRequest) -> None:
        required = {
            "operation_id": request.operation_id,
            "bridge_id": request.bridge_id,
            "root_id": request.root_id,
            "kind": request.kind,
            "idempotency_key": request.idempotency_key,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise InvalidLocalOperationRequest(
                f"local operation request requires non-empty {', '.join(missing)}"
            )

    def _entry(self, operation_id: str) -> _Entry[Any]:
        entry = self._entries.get(operation_id)
        if entry is None:
            raise UnknownLocalOperation(
                f"no local operation for operation_id={operation_id!r}"
            )
        return entry

    def _pending_entry(self, operation_id: str) -> _Entry[Any]:
        entry = self._entry(operation_id)
        if entry.status is not LocalOperationStatus.PENDING:
            raise LocalOperationAlreadyResolved(
                f"local operation for operation_id={operation_id!r} is already "
                f"{entry.status.value}"
            )
        return entry


__all__ = [
    "COMPLETE_LOCAL_OPERATION_UPDATE",
    "PENDING_LOCAL_OPERATIONS_QUERY",
    "DuplicateLocalOperation",
    "InvalidLocalOperationRequest",
    "LocalOperationAck",
    "LocalOperationAlreadyResolved",
    "LocalOperationFailed",
    "LocalOperationOutcome",
    "LocalOperationProtocolError",
    "LocalOperationRequest",
    "LocalOperationResult",
    "LocalOperationStatus",
    "LocalOperations",
    "MalformedLocalOperationResult",
    "UnknownLocalOperation",
]
