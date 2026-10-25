# ABOUTME: Tests that the Gemini plugin's activities turn the google-genai SDK's own retries
# off, so one activity attempt makes exactly one HTTP request and Temporal's retry policy is
# the only retry owner. The client is a real genai.Client configured to retry, pointed at a
# local HTTP server that answers 503 and counts requests (no network).
#
# Run with: uv run --frozen python -m pytest tests/ai_sdks/google_genai_plugin/test_sdk_retries_off.py -v

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from google.genai import Client as GeminiClient
from google.genai import errors as genai_errors
from google.genai._interactions import APIStatusError
from google.genai import types
from google.genai.types import HttpOptions, HttpRetryOptions
from temporalio.testing import ActivityEnvironment

from temporal_agent_harness.ai_sdks.google_genai_plugin._gemini_activity import (
    GeminiApiCaller,
)
from temporal_agent_harness.ai_sdks.google_genai_plugin._interactions_activity import (
    make_gemini_interactions_create_streamed,
)
from temporal_agent_harness.ai_sdks.google_genai_plugin._models import (
    _GeminiApiRequest,
    _GeminiUploadFileRequest,
    _GeminiUploadToFileSearchStoreRequest,
)

_UNAVAILABLE = json.dumps(
    {"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}
).encode()


class _Unavailable(BaseHTTPRequestHandler):
    """Answers every request 503 and counts it on the server."""

    def _answer(self) -> None:
        self.server.requests += 1  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_UNAVAILABLE)))
        self.end_headers()
        self.wfile.write(_UNAVAILABLE)

    do_GET = do_POST = _answer

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[ThreadingHTTPServer]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Unavailable)
    httpd.requests = 0  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


def _retrying_client(server: ThreadingHTTPServer) -> GeminiClient:
    """A client that, left to itself, would retry a 503 several times."""
    host, port = server.server_address[:2]
    return GeminiClient(
        api_key="test-key",
        http_options=HttpOptions(
            base_url=f"http://{host!s}:{port}",
            retry_options=HttpRetryOptions(attempts=4, initial_delay=0.01, max_delay=0.01),
        ),
    )


async def test_interactions_activity_makes_one_request_on_a_503(server: ThreadingHTTPServer):
    activity_fn = make_gemini_interactions_create_streamed(_retrying_client(server))

    with pytest.raises(APIStatusError) as caught:
        await ActivityEnvironment().run(
            activity_fn, {"model": "gemini-x", "input": "Hi", "stream": True}, None
        )

    assert caught.value.status_code == 503
    assert server.requests == 1  # type: ignore[attr-defined]


_API_REQUEST = _GeminiApiRequest(
    http_method="post", path="models/gemini-x:generateContent", request_dict={}
)
_CALLER_ACTIVITY_INPUTS: dict[str, Any] = {
    "gemini_api_client_async_request": _API_REQUEST,
    "gemini_api_client_async_request_streamed": _API_REQUEST,
    "gemini_files_upload": _GeminiUploadFileRequest(
        file_bytes=b"data", config=types.UploadFileConfig(mime_type="text/plain")
    ),
    "gemini_file_search_stores_upload": _GeminiUploadToFileSearchStoreRequest(
        file_search_store_name="fileSearchStores/s",
        file_bytes=b"data",
        config=types.UploadToFileSearchStoreConfig(mime_type="text/plain"),
    ),
}


@pytest.mark.parametrize("activity_name", sorted(_CALLER_ACTIVITY_INPUTS))
async def test_caller_activity_makes_one_request_on_a_503(
    server: ThreadingHTTPServer, activity_name: str
):
    activities = GeminiApiCaller(_retrying_client(server)).activities()
    activity_fn = next(fn for fn in activities if fn.__name__ == activity_name)

    with pytest.raises(genai_errors.ServerError) as caught:
        await ActivityEnvironment().run(activity_fn, _CALLER_ACTIVITY_INPUTS[activity_name])

    assert caught.value.code == 503
    assert server.requests == 1  # type: ignore[attr-defined]
