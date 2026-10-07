# ABOUTME: A deterministic fake of the OpenAI Responses API (SSE) for testing Codex agents with no
# credentials. Codex talks to it through a custom `model_provider`, so a test drives the REAL
# `codex app-server` binary and the whole adapter. Dependency-free (asyncio only) so it can ship
# in the package and back the credential-free example.

"""A fake Responses API for Codex agents.

The "model" is a script embedded in the user's text: each line ``CALL:<tool>|<json args>`` is one
tool call, issued one per round in order. ``{{out:N}}`` in the arguments is replaced by the N-th
tool output received so far (so a script can use a handle an earlier call returned). After the last
call the model answers ``RESULTS: <outputs joined by " ## ">``; with no ``CALL:`` lines it answers
``hello``. The script is read from the whole conversation, so use one scripted turn per thread.
Pair it with the plugin::

    fake = FakeResponsesServer()
    await fake.start()
    plugin = CodexHarnessPlugin(config_overrides=fake.config_overrides)

Every request body is kept in ``fake.requests`` so a test can assert what the model was sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from typing import Any

__all__ = ["FakeResponsesServer"]


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _user_text(items: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for item in items:
        if item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, list):
                out += [p.get("text", "") for p in content if isinstance(p, dict)]
    return "\n".join(out)


class FakeResponsesServer:
    """A local ``POST /v1/responses`` endpoint speaking just enough SSE for Codex."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._counter = itertools.count(1)
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def config_overrides(self) -> list[str]:
        """``--config`` overrides that point Codex at this server (pass to ``CodexHarnessPlugin``)."""
        return [
            'model_providers.fake={name="fake", base_url="http://127.0.0.1:%d/v1", '
            'wire_api="responses", requires_openai_auth=false}' % self.port,
            'model_provider="fake"',
            'model="fake-model"',
        ]

    @staticmethod
    def _output_text(item: dict[str, Any]) -> str:
        out = item.get("output")
        return out if isinstance(out, str) else json.dumps(out)

    # -- HTTP ---------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode()
            headers: dict[str, str] = {}
            while True:
                line = (await reader.readline()).decode().strip()
                if not line:
                    break
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()
            if headers.get("transfer-encoding", "").lower() == "chunked":
                body = b""
                while True:
                    size = int((await reader.readline()).strip() or b"0", 16)
                    if size == 0:
                        await reader.readline()
                        break
                    body += await reader.readexactly(size)
                    await reader.readline()
            else:
                body = await reader.readexactly(int(headers.get("content-length", "0")))
            if not request_line.startswith("POST") or "/responses" not in request_line:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            await self._responses(json.loads(body or b"{}"), writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _responses(self, body: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        n = next(self._counter)
        self.requests.append(body)
        items: list[dict[str, Any]] = body.get("input", [])
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
        )
        writer.write(_sse("response.created", {"type": "response.created", "response": {"id": f"resp_{n}", "output": []}}))
        outputs = [i for i in items if i.get("type") == "function_call_output"]
        output_texts = [self._output_text(i) for i in outputs]
        script = [
            line[len("CALL:") :].split("|", 1)
            for line in _user_text(items).splitlines()
            if line.startswith("CALL:") and "|" in line
        ]
        calls: list[list[str]] = []
        if len(outputs) < len(script):
            name, args = script[len(outputs)]
            for i, text in enumerate(output_texts):
                args = args.replace("{{out:%d}}" % i, text)
            calls = [[name, args]]
        if calls:
            for k, (name, args) in enumerate(calls):
                item = {
                    "type": "function_call",
                    "id": f"fc_{n}_{k}",
                    "call_id": f"call_{n}_{k}",
                    "name": name,
                    "arguments": args.strip(),
                }
                writer.write(_sse("response.output_item.added", {"type": "response.output_item.added", "output_index": k, "item": item}))
                writer.write(_sse("response.output_item.done", {"type": "response.output_item.done", "output_index": k, "item": item}))
        else:
            text = "RESULTS: " + " ## ".join(output_texts) if outputs else "hello"
            message = {
                "type": "message",
                "role": "assistant",
                "id": f"msg_{n}",
                "content": [{"type": "output_text", "text": text}],
            }
            writer.write(
                _sse("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {**message, "content": []}})
            )
            half = max(1, len(text) // 2)
            for chunk in (text[:half], text[half:]):
                if chunk:
                    writer.write(
                        _sse(
                            "response.output_text.delta",
                            {
                                "type": "response.output_text.delta",
                                "item_id": f"msg_{n}",
                                "output_index": 0,
                                "content_index": 0,
                                "delta": chunk,
                            },
                        )
                    )
            writer.write(_sse("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": message}))
        usage = {"input_tokens": 11, "input_tokens_details": None, "output_tokens": 7, "output_tokens_details": None, "total_tokens": 18}
        writer.write(_sse("response.completed", {"type": "response.completed", "response": {"id": f"resp_{n}", "output": [], "usage": usage}}))
        await writer.drain()

