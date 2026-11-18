"""The one HTTP call the built-in data sources make: JSON in, JSON out, with the standard
library, so they add no dependency. A source takes a ``send`` function so that a test can
answer in place of the network."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from temporalio.exceptions import ApplicationError

# (method, url, headers, JSON body or None) -> (status, body)
Send = Callable[[str, str, Mapping[str, str], bytes | None], Awaitable[tuple[int, bytes]]]

TIMEOUT_SECONDS = 30


def _send_blocking(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


async def send(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    return await asyncio.to_thread(_send_blocking, method, url, headers, body)


async def request_json(
    source: str,
    send_fn: Send,
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Any = None,
) -> Any:
    """The JSON a request returns. 404 gives ``None``. Another client error fails without a
    retry, since the same request would fail again. A 429 or a server error is retried."""
    sent = {"Accept": "application/json", **headers}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        sent["Content-Type"] = "application/json"
    status, content = await send_fn(method, url, sent, data)
    if status == 404:
        return None
    if status == 429 or status >= 500:
        raise RuntimeError(f"The {source} data source got HTTP {status}.")
    if status >= 400:
        raise ApplicationError(
            f"The {source} data source got HTTP {status}.",
            type="DataSourceRefused",
            non_retryable=True,
        )
    return json.loads(content) if content else None
