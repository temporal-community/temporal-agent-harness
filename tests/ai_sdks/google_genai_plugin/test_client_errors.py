# ABOUTME: Tests that the Gemini plugin's activities fail a Gemini client error (4xx other
# than 408/429) NON-retryably, so a bad request ends the agent's turn instead of being
# retried forever, while 5xx, 408, 429 and network errors propagate unchanged and stay
# retryable. Covers both SDK exception families: the Interactions API's generated client
# (google.genai._interactions.APIStatusError) and BaseApiClient's
# google.genai.errors.ClientError/ServerError (generate_content, files, file search stores).
#
# The SDK client is faked; the exceptions it raises are built by the SDK's own
# status -> exception mapping, so they are the classes and messages a real reply produces.
#
# Run with: uv run --frozen python -m pytest tests/ai_sdks/google_genai_plugin/test_client_errors.py -v

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from google.genai import Client as GeminiClient
from google.genai import errors as genai_errors
from google.genai._interactions import (
    APIConnectionError,
    APIStatusError,
    AsyncGeminiNextGenAPIClient,
)
from google.genai.types import HttpResponse as SdkHttpResponse
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.google_genai_plugin._gemini_activity import (
    GeminiApiCaller,
)
from temporal_agent_harness.ai_sdks.google_genai_plugin._interactions_activity import (
    make_gemini_interactions_create_streamed,
)
from temporal_agent_harness.ai_sdks.google_genai_plugin._models import (
    _GeminiApiRequest,
    _GeminiDownloadFileRequest,
    _GeminiRegisterFilesRequest,
    _GeminiUploadFileRequest,
    _GeminiUploadToFileSearchStoreRequest,
)

# The opening sentence of the 400 a live Interactions call returned, which motivated this.
_TURN_ORDER_MESSAGE = (
    "Please ensure that function response turn comes immediately after a function "
    "call turn."
)

_INVALID_ARGUMENT_MESSAGE = "Request contains an invalid argument."

NON_RETRYABLE_STATUSES = [400, 401, 403, 404, 409, 422]
RETRYABLE_STATUSES = [408, 429, 500, 503]


def _interactions_error(status_code: int) -> APIStatusError:
    """What ``client.aio.interactions.create`` raises for an HTTP ``status_code`` reply."""
    response = httpx.Response(
        status_code,
        json={"error": {"message": _TURN_ORDER_MESSAGE, "code": "invalid_request"}},
        request=httpx.Request(
            "POST", "https://generativelanguage.googleapis.com/v1beta/interactions"
        ),
    )
    sdk = AsyncGeminiNextGenAPIClient(api_key="test-key")
    return sdk._make_status_error_from_response(response)


def _genai_error(status_code: int) -> genai_errors.APIError:
    """What a ``BaseApiClient`` call raises for an HTTP ``status_code`` reply."""
    body = {
        "error": {
            "code": status_code,
            "message": _INVALID_ARGUMENT_MESSAGE,
            "status": "INVALID_ARGUMENT",
        }
    }
    try:
        genai_errors.APIError.raise_error(status_code, body, None)
    except genai_errors.APIError as e:
        return e
    raise AssertionError("APIError.raise_error did not raise")


def _interactions_activity(raises: BaseException) -> tuple[Any, AsyncMock]:
    """The real interactions activity over a client whose ``create`` raises ``raises``."""
    create = AsyncMock(side_effect=raises)
    client = SimpleNamespace(
        aio=SimpleNamespace(interactions=SimpleNamespace(create=create))
    )
    return make_gemini_interactions_create_streamed(cast(GeminiClient, client)), create


def _assert_non_retryable(
    raised: BaseException, original: BaseException, status_code: int, message: str
) -> None:
    assert isinstance(raised, ApplicationError)
    assert raised.non_retryable
    # The type Temporal would have reported for the raw SDK exception.
    assert raised.type == type(original).__name__
    assert str(status_code) in str(raised)
    assert message in str(raised)
    assert raised.details == ({"status_code": status_code, "message": message},)
    assert raised.__cause__ is original


# ------------------------------------------------------------ Interactions API activity


@pytest.mark.parametrize("status_code", NON_RETRYABLE_STATUSES)
async def test_interactions_client_error_is_non_retryable(status_code: int):
    original = _interactions_error(status_code)
    activity_fn, _ = _interactions_activity(original)

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(activity_fn, {"model": "gemini-x"}, None)

    # The API's own message, not the SDK's "Error code: 400 - {<whole body>}" wrapper.
    _assert_non_retryable(caught.value, original, status_code, _TURN_ORDER_MESSAGE)
    assert "Error code" not in caught.value.message


@pytest.mark.parametrize("status_code", RETRYABLE_STATUSES)
async def test_interactions_transient_error_stays_retryable(status_code: int):
    original = _interactions_error(status_code)
    activity_fn, _ = _interactions_activity(original)

    with pytest.raises(APIStatusError) as caught:
        await ActivityEnvironment().run(activity_fn, {"model": "gemini-x"}, None)

    assert caught.value is original


async def test_interactions_network_error_stays_retryable():
    original = APIConnectionError(
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com")
    )
    activity_fn, _ = _interactions_activity(original)

    with pytest.raises(APIConnectionError) as caught:
        await ActivityEnvironment().run(activity_fn, {"model": "gemini-x"}, None)

    assert caught.value is original


@workflow.defn(sandboxed=False)
class _InteractionsCallWorkflow:
    """Calls the interactions activity once, allowing Temporal up to three attempts."""

    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            "gemini_interactions_create_streamed",
            args=[{"model": "gemini-x"}, None],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=1), maximum_attempts=3
            ),
        )


@pytest.mark.parametrize(
    ("status_code", "expected_attempts", "expected_non_retryable"),
    [(400, 1, True), (503, 3, False)],
)
async def test_interactions_retry_posture_on_a_real_worker(
    status_code: int, expected_attempts: int, expected_non_retryable: bool
):
    """The end-to-end claim: Temporal stops at the first attempt for a 400, but keeps
    retrying a 503 until the retry policy is exhausted."""
    activity_fn, create = _interactions_activity(_interactions_error(status_code))
    task_queue = f"gemini-client-errors-{uuid.uuid4()}"

    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_InteractionsCallWorkflow],
            activities=[activity_fn],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            with pytest.raises(WorkflowFailureError) as caught:
                await env.client.execute_workflow(
                    _InteractionsCallWorkflow.run,
                    id=f"gemini-client-errors-{uuid.uuid4()}",
                    task_queue=task_queue,
                )

    assert create.await_count == expected_attempts
    activity_error = caught.value.cause
    assert isinstance(activity_error, ActivityError)
    cause = activity_error.cause
    assert isinstance(cause, ApplicationError)
    assert cause.non_retryable is expected_non_retryable
    assert _TURN_ORDER_MESSAGE in str(cause)


# ------------------------------------------- generate_content / files / file-search activities


def _caller(raises: BaseException) -> dict[str, Any]:
    """Every ``GeminiApiCaller`` activity, by name, over a client whose SDK calls raise."""
    client = SimpleNamespace(
        aio=SimpleNamespace(
            _api_client=SimpleNamespace(
                async_request=AsyncMock(side_effect=raises),
                async_request_streamed=AsyncMock(side_effect=raises),
            ),
            files=SimpleNamespace(
                upload=AsyncMock(side_effect=raises),
                download=AsyncMock(side_effect=raises),
                register_files=AsyncMock(side_effect=raises),
            ),
            file_search_stores=SimpleNamespace(
                upload_to_file_search_store=AsyncMock(side_effect=raises),
            ),
        )
    )
    caller = GeminiApiCaller(cast(GeminiClient, client), credentials=cast(Any, object()))
    return {fn.__name__: fn for fn in caller.activities()}


_API_REQUEST = _GeminiApiRequest(
    http_method="post", path="models/gemini-x:generateContent", request_dict={}
)
_CALLER_ACTIVITY_INPUTS = {
    "gemini_api_client_async_request": _API_REQUEST,
    "gemini_api_client_async_request_streamed": _API_REQUEST,
    "gemini_files_upload": _GeminiUploadFileRequest(file_bytes=b"data"),
    "gemini_files_download": _GeminiDownloadFileRequest(file="files/abc"),
    "gemini_files_register": _GeminiRegisterFilesRequest(uris=["gs://bucket/a"]),
    "gemini_file_search_stores_upload": _GeminiUploadToFileSearchStoreRequest(
        file_search_store_name="fileSearchStores/s", file_bytes=b"data"
    ),
}


def test_every_caller_activity_is_covered():
    """A new activity on GeminiApiCaller must be added to these tests too."""
    names = set(_caller(RuntimeError("unused")))
    assert names == {*_CALLER_ACTIVITY_INPUTS, "gemini_interactions_create_streamed"}


@pytest.mark.parametrize("activity_name", sorted(_CALLER_ACTIVITY_INPUTS))
@pytest.mark.parametrize("status_code", NON_RETRYABLE_STATUSES)
async def test_caller_client_error_is_non_retryable(activity_name: str, status_code: int):
    original = _genai_error(status_code)
    activity_fn = _caller(original)[activity_name]

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(
            activity_fn, _CALLER_ACTIVITY_INPUTS[activity_name]
        )

    assert isinstance(original, genai_errors.ClientError)
    _assert_non_retryable(
        caught.value, original, status_code, _INVALID_ARGUMENT_MESSAGE
    )


@pytest.mark.parametrize("activity_name", sorted(_CALLER_ACTIVITY_INPUTS))
@pytest.mark.parametrize("status_code", RETRYABLE_STATUSES)
async def test_caller_transient_error_stays_retryable(
    activity_name: str, status_code: int
):
    original = _genai_error(status_code)
    activity_fn = _caller(original)[activity_name]

    with pytest.raises(genai_errors.APIError) as caught:
        await ActivityEnvironment().run(
            activity_fn, _CALLER_ACTIVITY_INPUTS[activity_name]
        )

    assert caught.value is original


def _streamed_activity_failing_mid_stream(error: BaseException) -> Any:
    """The streamed generate_content activity over a stream that yields one chunk and
    then raises ``error``, the way the SDK raises an error chunk mid-stream."""

    async def chunks() -> AsyncIterator[SdkHttpResponse]:
        yield SdkHttpResponse(headers={}, body='{"candidates": []}')
        raise error

    client = SimpleNamespace(
        aio=SimpleNamespace(
            _api_client=SimpleNamespace(
                async_request_streamed=AsyncMock(return_value=chunks())
            )
        )
    )
    activities = GeminiApiCaller(cast(GeminiClient, client)).activities()
    return next(
        fn
        for fn in activities
        if fn.__name__ == "gemini_api_client_async_request_streamed"
    )


async def test_streamed_request_mid_stream_client_error_is_non_retryable():
    """The request itself succeeded; the 4xx arrives while the stream is iterated."""
    original = _genai_error(400)
    activity_fn = _streamed_activity_failing_mid_stream(original)

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(activity_fn, _API_REQUEST)

    _assert_non_retryable(
        caught.value, original, 400, _INVALID_ARGUMENT_MESSAGE
    )


@pytest.mark.parametrize("status_code", [429, 503])
async def test_streamed_request_mid_stream_transient_error_stays_retryable(
    status_code: int,
):
    original = _genai_error(status_code)
    activity_fn = _streamed_activity_failing_mid_stream(original)

    with pytest.raises(genai_errors.APIError) as caught:
        await ActivityEnvironment().run(activity_fn, _API_REQUEST)

    assert caught.value is original
