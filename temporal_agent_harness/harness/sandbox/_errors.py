"""Carrying an OpenAI ``SandboxError`` across the activity boundary, so workflow-side code can
catch the same error class the sandbox raised on the worker."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from agents.sandbox.errors import ErrorCode, SandboxError
from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from temporalio.exceptions import ActivityError, ApplicationError


class SandboxErrorDetails(BaseModel):
    """A ``SandboxError``'s fields, as the details of the ``ApplicationError`` it becomes."""

    error_class: str
    message: str
    error_code: str
    op: str
    context: dict[str, Any]
    retryable: bool | None

    @classmethod
    def of(cls, error: SandboxError) -> SandboxErrorDetails:
        return cls(
            error_class=f"{type(error).__module__}:{type(error).__qualname__}",
            message=error.message,
            error_code=ErrorCode(error.error_code).value,
            op=str(error.op),
            context=to_jsonable_python(error.context, fallback=str),
            retryable=error.retryable,
        )


@contextmanager
def translate_sandbox_errors() -> Iterator[None]:
    """Turn a ``SandboxError`` into an ``ApplicationError`` carrying its fields.

    Temporal retries every activity exception by default, so only a ``SandboxError`` the
    library has classified as terminal (``retryable`` is False) is non-retryable.
    """
    try:
        yield
    except SandboxError as e:
        raise ApplicationError(
            str(e),
            SandboxErrorDetails.of(e).model_dump(mode="json"),
            type=str(e.error_code),
            non_retryable=e.retryable is False,
        ) from e


def sandbox_error_from(error: ActivityError) -> SandboxError | None:
    """The ``SandboxError`` an activity failed with, rebuilt as its own class, or ``None``
    when the failure was something else."""
    cause = error.cause
    if not isinstance(cause, ApplicationError) or not cause.details:
        return None
    try:
        details = SandboxErrorDetails.model_validate(cause.details[0])
    except ValueError:
        return None
    module, _, qualname = details.error_class.partition(":")
    error_class: Any = importlib.import_module(module) if module.startswith("agents.") else None
    for part in qualname.split(".") if error_class is not None else ():
        error_class = getattr(error_class, part, None)
    if not (isinstance(error_class, type) and issubclass(error_class, SandboxError)):
        error_class = SandboxError
    # Subclass constructors take differing arguments, so the instance is built through the
    # base dataclass's; subclass fields (``session_id``, ``timeout_s``) are in the context.
    rebuilt = error_class.__new__(error_class)
    SandboxError.__init__(
        rebuilt,
        message=details.message,
        error_code=ErrorCode(details.error_code),
        op=details.op,  # type: ignore[arg-type]
        context=details.context,
        retryable=details.retryable,
    )
    for name, value in details.context.items():
        if any(name in getattr(c, "__annotations__", {}) for c in error_class.__mro__):
            setattr(rebuilt, name, value)
    rebuilt.__cause__ = error
    return rebuilt
