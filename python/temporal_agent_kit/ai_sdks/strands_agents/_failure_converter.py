"""Failure converter for Strands-specific exceptions."""

from strands.interrupt import InterruptException
from strands.types.exceptions import (
    ContextWindowOverflowException,
    MaxTokensReachedException,
    SessionException,
    StructuredOutputException,
)

import temporalio.api.failure.v1
from temporalio.converter import (
    DefaultFailureConverter,
    DefaultFailureConverterWithEncodedAttributes,
    FailureConverter,
    PayloadConverter,
)
from temporalio.exceptions import ApplicationError

# Activity-side: when a Strands ``InterruptException`` would otherwise be
# serialized by the default converter, the ``Interrupt`` payload on
# ``exc.interrupt`` is dropped (it lives on the instance, not in the
# serialized ApplicationError). We translate to a typed ApplicationError so
# the interrupt data survives the activity boundary and the workflow side
# can rebuild a real ``Interrupt``.
STRANDS_INTERRUPT_TYPE = "StrandsInterrupt"

# Strands' model/session exceptions that are deterministic failures (token
# limits, context overflow, structured-output validation, session I/O). They
# won't succeed on retry, so they cross the boundary as non-retryable typed
# ApplicationErrors. Only ContextWindowOverflowException is rebuilt on the workflow
# side (TemporalModel.stream), because Strands recovers from it by reducing the
# conversation; the others reach the workflow as an ActivityError.
_TERMINAL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    MaxTokensReachedException,
    ContextWindowOverflowException,
    StructuredOutputException,
    SessionException,
)


class StrandsFailureConverter(DefaultFailureConverter):
    """Failure converter that preserves Strands exception payloads and retryability."""

    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
    ) -> None:
        """Translate Strands exceptions to typed ApplicationErrors."""
        if isinstance(exception, InterruptException):
            super().to_failure(
                ApplicationError(
                    f"interrupt:{exception.interrupt.name}",
                    exception.interrupt.to_dict(),
                    type=STRANDS_INTERRUPT_TYPE,
                    non_retryable=True,
                ),
                payload_converter,
                failure,
            )
            return
        if isinstance(exception, _TERMINAL_EXCEPTIONS):
            super().to_failure(
                ApplicationError(
                    str(exception),
                    type=type(exception).__name__,
                    non_retryable=True,
                ),
                payload_converter,
                failure,
            )
            return
        super().to_failure(exception, payload_converter, failure)


# StrandsFailureConverter has no __init__, so the one from
# DefaultFailureConverterWithEncodedAttributes runs and turns on the encoding.
class StrandsFailureConverterWithEncodedAttributes(  # pyright: ignore[reportUnsafeMultipleInheritance]
    StrandsFailureConverter, DefaultFailureConverterWithEncodedAttributes
):
    """``StrandsFailureConverter`` that moves message and stack trace to encoded attributes.

    Both Strands converters decode with ``DefaultFailureConverter.from_failure``, which
    reads encoded and plain attributes, so a Replayer with ``StrandsFailureConverter``
    still reads the failures this class writes.
    """


def strands_failure_converter_class(
    base: type[FailureConverter],
) -> type[StrandsFailureConverter]:
    """Return the Strands failure converter class to use in place of ``base``.

    A subclass of ``DefaultFailureConverterWithEncodedAttributes`` becomes
    ``StrandsFailureConverterWithEncodedAttributes``, so failure messages and stack
    traces stay encoded. Every other class is replaced with
    ``StrandsFailureConverter``, including one that sets ``encode_common_attributes``
    in its own ``__init__``. The caller's own ``to_failure`` and ``from_failure``
    never run, so the failures that Workflow code reads stay the same.
    """
    if isinstance(base, type) and issubclass(
        base, DefaultFailureConverterWithEncodedAttributes
    ):
        return StrandsFailureConverterWithEncodedAttributes
    return StrandsFailureConverter


def strands_failure_converter_class_keeping_caller(
    base: type[FailureConverter],
) -> type[StrandsFailureConverter]:
    """Return a converter class that adds the Strands handling on top of ``base``.

    Used when the caller supplied their own payload converter, where their failure
    converter was always kept. The caller's ``from_failure`` (and ``to_failure`` for
    every other exception) still runs, so recorded failures decode as before. A
    converter that is not a ``DefaultFailureConverter`` subclass is returned
    unchanged, without the Strands handling.
    """
    if isinstance(base, type) and issubclass(base, StrandsFailureConverter):
        # Already wrapped by an earlier run of the plugin.
        return base
    if not isinstance(base, type) or not issubclass(base, DefaultFailureConverter):
        # A failure converter that does not build on the default one cannot take
        # the Strands handling; keep it exactly as the caller supplied it.
        return base  # type: ignore[return-value]
    if base in (DefaultFailureConverter, DefaultFailureConverterWithEncodedAttributes):
        return strands_failure_converter_class(base)
    return type(  # type: ignore[return-value]
        f"Strands{base.__name__}", (StrandsFailureConverter, base), {}
    )
