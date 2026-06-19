"""Temporal activity that executes Gemini SDK API calls with real credentials.

The ``TemporalApiClient`` in the workflow dispatches calls here. This
activity holds a user-provided ``genai.Client`` and forwards structured
requests. Credentials are fetched/refreshed only within the activity —
they never appear in workflow event history.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any, Callable

import google.auth.credentials
from google.genai import Client as GeminiClient
from google.genai import types
from google.genai.types import HttpOptions
from google.genai.types import HttpResponse as SdkHttpResponse

from temporalio import activity

from temporal_agent_harness.harness.agent_protocol import ReplyDelta
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner
from ._interactions_activity import make_gemini_interactions_create_streamed
from ._models import (
    _GeminiApiRequest,
    _GeminiApiResponse,
    _GeminiApiStreamedResponse,
    _GeminiDownloadFileRequest,
    _GeminiRegisterFilesRequest,
    _GeminiUploadFileRequest,
    _GeminiUploadToFileSearchStoreRequest,
)


def _extract_text_delta(body: str) -> str:
    """Pull the prose text out of a single streamed Gemini response chunk.

    Chunks come in as raw HTTP response bodies — usually a JSON object
    representing one partial ``GenerateContentResponse``, sometimes
    prefixed with an SSE ``data:`` field. Tolerant of both shapes;
    returns ``""`` for non-text chunks (function-call parts, malformed
    bodies, etc.).
    """
    body = body.strip()
    if body.startswith("data:"):
        body = body[5:].strip()
    if not body:
        return ""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    text = ""
    for cand in data.get("candidates", []) or []:
        content = cand.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    text += t
    return text


def _resolve_http_options(
    overrides: Any,
) -> HttpOptions | None:
    """Reconstruct ``HttpOptions`` from serializable overrides, or None."""
    if overrides is None:
        return None
    return HttpOptions.model_validate(overrides.model_dump(exclude_none=True))


class GeminiApiCaller:
    """Wraps a ``genai.Client`` and exposes Temporal activities for SDK calls.

    The caller owns a reference to the user-provided ``genai.Client``.
    All credential management, HTTP client configuration, etc. is the
    responsibility of whoever constructs the client.
    """

    def __init__(
        self,
        client: GeminiClient,
        credentials: google.auth.credentials.Credentials | None = None,
    ) -> None:
        """Initialize with a genai.Client and optional extra credentials."""
        self._client = client
        self._credentials = credentials

    def activities(self) -> Sequence[Callable]:
        """Return activities that route SDK calls through this client."""

        @activity.defn
        async def gemini_api_client_async_request(
            req: _GeminiApiRequest,
        ) -> _GeminiApiResponse:
            """Execute a Gemini SDK API call with real credentials."""
            response: SdkHttpResponse = (
                await self._client.aio._api_client.async_request(
                    http_method=req.http_method,
                    path=req.path,
                    request_dict=req.request_dict,
                    http_options=_resolve_http_options(req.http_options_overrides),
                )
            )
            return _GeminiApiResponse(
                headers=response.headers or {},
                body=response.body or "",
            )

        @activity.defn
        async def gemini_api_client_async_request_streamed(
            req: _GeminiApiRequest,
        ) -> _GeminiApiStreamedResponse:
            """Execute a streamed Gemini SDK API call, collecting all chunks.

            When ``req.stream_turn_id`` is set, each chunk's text content is
            republished as a ``reply_delta`` event on the parent workflow's
            :class:`WorkflowStream` as soon as it arrives — fine-grained,
            visible to the UI in real time. Function-call chunks (no text
            parts) are silently skipped.
            """
            stream = await self._client.aio._api_client.async_request_streamed(
                http_method=req.http_method,
                path=req.path,
                request_dict=req.request_dict,
                http_options=_resolve_http_options(req.http_options_overrides),
            )

            chunks: list[_GeminiApiResponse] = []

            # If a stream context rode in on the request, hand it to the
            # harness's publisher helper. The activity never unpacks
            # turn_id/turn_number itself — it just forwards the opaque
            # carrier and calls ``publish`` per chunk.
            async with AsyncExitStack() as stack:
                publisher = None
                if req.stream_context is not None:
                    publisher = await stack.enter_async_context(
                        AgentWorkflowRunner.publisher_from_activity(
                            req.stream_context,
                        )
                    )

                async for chunk in stream:
                    body = chunk.body or ""
                    chunks.append(
                        _GeminiApiResponse(headers=chunk.headers or {}, body=body)
                    )
                    if publisher is not None:
                        delta = _extract_text_delta(body)
                        if delta:
                            publisher.publish(ReplyDelta(text=delta))

            return _GeminiApiStreamedResponse(chunks=chunks)

        @activity.defn
        async def gemini_files_upload(
            req: _GeminiUploadFileRequest,
        ) -> types.File:
            """Upload a file using the real genai.Client on the worker."""
            if req.file_bytes is not None:
                import io

                file_arg: Any = io.BytesIO(req.file_bytes)
            else:
                file_arg = req.file_path

            return await self._client.aio.files.upload(file=file_arg, config=req.config)

        @activity.defn
        async def gemini_files_download(
            req: _GeminiDownloadFileRequest,
        ) -> bytes:
            """Download a file using the real genai.Client on the worker."""
            return await self._client.aio.files.download(
                file=req.file, config=req.config
            )

        @activity.defn
        async def gemini_files_register(
            req: _GeminiRegisterFilesRequest,
        ) -> types.RegisterFilesResponse:
            """Register GCS files using the real genai.Client on the worker.

            Uses ``credentials`` if provided at plugin init,
            otherwise falls back to the client's own credentials.
            Token refresh happens here on the worker side, so no auth
            material enters the workflow event history.
            """
            auth = self._credentials or self._client._api_client._credentials
            if auth is None:
                raise ValueError(
                    "No credentials available for register_files(). "
                    "Pass extra_credentials to GoogleGenAIPlugin or initialize "
                    "the genai.Client with credentials."
                )
            return await self._client.aio.files.register_files(
                auth=auth,
                uris=req.uris,
                config=req.config,
            )

        @activity.defn
        async def gemini_file_search_stores_upload(
            req: _GeminiUploadToFileSearchStoreRequest,
        ) -> types.UploadToFileSearchStoreOperation:
            """Upload a file to a file search store on the worker."""
            if req.file_bytes is not None:
                import io

                file_arg: Any = io.BytesIO(req.file_bytes)
            else:
                file_arg = req.file_path

            return (
                await self._client.aio.file_search_stores.upload_to_file_search_store(
                    file_search_store_name=req.file_search_store_name,
                    file=file_arg,
                    config=req.config,
                )
            )

        return [
            gemini_api_client_async_request,
            gemini_api_client_async_request_streamed,
            gemini_files_upload,
            gemini_files_download,
            gemini_files_register,
            gemini_file_search_stores_upload,
            make_gemini_interactions_create_streamed(self._client),
        ]
