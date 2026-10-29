"""Retry posture for Gemini API failures raised inside the plugin's activities.

Temporal retries every activity exception by default. A 4xx from the Gemini API says
the request itself is wrong — a malformed turn order, a bad argument, a missing file, a
rejected key — so retrying the identical request fails the same way forever, with
backoff, and the agent's turn hangs instead of failing. :func:`translate_gemini_errors`
turns those into non-retryable :class:`ApplicationError` s carrying the status code and
the API's own message.

408 (Request Timeout) and 429 (Too Many Requests) are 4xx codes that report a transient
condition, so they propagate unchanged and stay retryable, as do 5xx and network errors.

The SDK raises two unrelated exception families. ``generate_content``, files and
file-search-store calls go through ``BaseApiClient`` and raise
``google.genai.errors.ClientError`` for every 4xx. The Interactions API is a separate
generated client (``google.genai._interactions``) whose 4xx/5xx errors are
``APIStatusError`` subclasses such as ``BadRequestError``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NoReturn

from google.genai import errors as genai_errors
from google.genai._interactions import APIStatusError as InteractionsStatusError
from temporalio.exceptions import ApplicationError

# 4xx codes that report a transient condition rather than a bad request.
_TRANSIENT_CLIENT_STATUS_CODES = frozenset({408, 429})


@contextmanager
def translate_gemini_errors() -> Iterator[None]:
    """Raise a Gemini client error from the body as a non-retryable ``ApplicationError``.

    The ``ApplicationError`` keeps the SDK exception's class name as its ``type`` (what
    Temporal would have reported for the raw exception), carries
    ``{"status_code", "message"}`` as its one detail, and chains the original as its
    cause. Every other exception, including retryable Gemini errors, propagates as is.
    """
    try:
        yield
    except genai_errors.ClientError as e:
        # The SDK raises ClientError for exactly the 4xx range, with ``code`` set to
        # the HTTP status.
        if e.code not in _TRANSIENT_CLIENT_STATUS_CODES:
            _raise_non_retryable(e, e.code, e.message or str(e))
        raise
    except InteractionsStatusError as e:
        if 400 <= e.status_code < 500 and (
            e.status_code not in _TRANSIENT_CLIENT_STATUS_CODES
        ):
            _raise_non_retryable(e, e.status_code, _interactions_api_message(e))
        raise


def _raise_non_retryable(e: Exception, status_code: int, message: str) -> NoReturn:
    raise ApplicationError(
        f"Gemini API returned {status_code}: {message}",
        {"status_code": status_code, "message": message},
        type=type(e).__name__,
        non_retryable=True,
    ) from e


def _interactions_api_message(e: InteractionsStatusError) -> str:
    """The API's ``error.message`` off an Interactions error body.

    The SDK's own ``message`` is ``"Error code: 400 - {<the whole decoded body>}"``;
    the body's ``error.message`` is the part that says what was wrong with the request.
    A body that did not decode to that shape (the SDK keeps raw text, or ``None`` when
    the response was unreadable) leaves the SDK's message as the only description.
    """
    body = e.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return e.message
