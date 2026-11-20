"""Credentials the gateway sends to 3rd-party MCP servers.

A registration carries only the *name* of a credential. The secret stays in the gateway
worker's environment and is read inside the activity that makes the HTTP request, so it never
enters workflow history, a signal, a query result or a Nexus payload.

The worker declares its credentials in ``GATEWAY_CREDENTIALS``, a JSON object::

    {"notion": {"url": "https://mcp.notion.com/", "token_env": "NOTION_TOKEN"}}

``token_env`` is the environment variable that holds the token. ``url`` is the only place the
token may be sent to: a server URL must have the same scheme, host and port, and a path under
this one. Without that check, anyone who can register a server could point a credential at a
server of their own and read the token.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client as MCPClient
from mcp.client.streamable_http import streamable_http_client
from temporalio.exceptions import ApplicationError

CREDENTIALS_ENV = "GATEWAY_CREDENTIALS"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class Credential:
    """A bearer token in ``token_env``, for servers under ``url``."""

    url: str
    token_env: str


def validate_credential_name(name: str) -> None:
    if not _NAME.fullmatch(name):
        raise ValueError(
            f"credential name {name!r} must be letters, digits, '-' and '_', like 'notion'"
        )


def load_credentials(environ: Mapping[str, str] | None = None) -> dict[str, Credential]:
    """The credentials this worker declares in ``GATEWAY_CREDENTIALS``."""
    raw = (os.environ if environ is None else environ).get(CREDENTIALS_ENV)
    if not raw:
        return {}
    declared = json.loads(raw)
    if not isinstance(declared, dict):
        raise ValueError(f"{CREDENTIALS_ENV} must be a JSON object")
    credentials: dict[str, Credential] = {}
    for name, entry in declared.items():
        validate_credential_name(name)
        if not isinstance(entry, dict) or set(entry) != {"url", "token_env"}:
            raise ValueError(
                f"{CREDENTIALS_ENV}[{name!r}] must be an object with 'url' and 'token_env'"
            )
        credentials[name] = Credential(url=str(entry["url"]), token_env=str(entry["token_env"]))
    return credentials


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    default = {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or default


def _covers(allowed: str, server_url: str) -> bool:
    if _origin(allowed) != _origin(server_url) or not _origin(allowed)[1]:
        return False
    prefix = urlsplit(allowed).path.rstrip("/")
    path = urlsplit(server_url).path
    return path == prefix or path.startswith(prefix + "/")


def _refused(message: str, kind: str) -> ApplicationError:
    # Not retried: the worker's environment does not change between attempts.
    return ApplicationError(message, type=kind, non_retryable=True)


def credential_headers(
    credential: str | None, server_url: str, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The headers that authenticate a request to ``server_url`` with ``credential``."""
    if not credential:
        return {}
    env = os.environ if environ is None else environ
    declared = load_credentials(env).get(credential)
    if declared is None:
        raise _refused(
            f"The gateway worker declares no credential {credential!r} in {CREDENTIALS_ENV}.",
            "UnknownCredential",
        )
    if not _covers(declared.url, server_url):
        raise _refused(
            f"Credential {credential!r} is for {declared.url}, not for {server_url}.",
            "CredentialNotAllowed",
        )
    token = env.get(declared.token_env)
    if not token:
        raise _refused(
            f"Credential {credential!r} reads {declared.token_env}, which is not set on the "
            f"gateway worker.",
            "MissingCredential",
        )
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def connect(server_url: str, credential: str | None = None) -> AsyncIterator[MCPClient]:
    """An MCP client for ``server_url``, with the credential's headers on every request."""
    headers = credential_headers(credential, server_url)
    if not headers:
        async with MCPClient(server_url, mode="auto") as client:
            yield client
        return
    # The timeouts the MCP SDK uses for a client it makes itself.
    timeout = httpx2.Timeout(30.0, read=300.0)
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
        transport = streamable_http_client(server_url, http_client=http)
        async with MCPClient(transport, mode="auto") as client:
            yield client
