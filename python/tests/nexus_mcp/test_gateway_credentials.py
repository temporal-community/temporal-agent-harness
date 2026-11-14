# ABOUTME: Tests for the Durable Tools Gateway's credentials: a registration names a credential,
# the gateway worker reads the token from its own environment and sends it only to the URL the
# credential is declared for, and the registry workflow keeps the name and never the token.

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import nexusrpc
import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from nexus_mcp.durable_tools_gateway.credentials import (
    CREDENTIALS_ENV,
    credential_headers,
    load_credentials,
)
from nexus_mcp.durable_tools_gateway.generated import CallToolInput, RegisterExternalInput
from nexus_mcp.durable_tools_gateway.registry import (
    RemoteServer,
    ToolRegistryWorkflow,
    fetch_external_tools,
)
from nexus_mcp.durable_tools_gateway.registry_service_handler import (
    ExternalMCPCallInput,
    RegistryServiceHandler,
    mcp_proxy_activity,
)
from temporalio import activity, nexus
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

TOKEN = "s3cret-token"

mcp = MCPServer("private-tools")


@mcp.tool()
def whoami() -> str:
    """Say who called."""
    return "an authorized caller"


def _declare(monkeypatch: pytest.MonkeyPatch, url: str, token: str | None = TOKEN) -> None:
    declared = {"private": {"url": url, "token_env": "PRIVATE_TOOLS_TOKEN"}}
    monkeypatch.setenv(CREDENTIALS_ENV, json.dumps(declared))
    if token is None:
        monkeypatch.delenv("PRIVATE_TOOLS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("PRIVATE_TOOLS_TOKEN", token)


# ---------------------------------------------------------------- which headers, for which URL


def test_no_credential_means_no_headers():
    assert credential_headers(None, "https://tools.example/mcp", {}) == {}


@pytest.mark.parametrize(
    "server_url",
    [
        "https://tools.example/mcp",
        "https://tools.example/mcp/v2",
        "https://TOOLS.example:443/mcp",
    ],
)
def test_a_credential_is_sent_to_a_server_under_its_url(server_url: str):
    env = {
        CREDENTIALS_ENV: '{"private": {"url": "https://tools.example/mcp", "token_env": "T"}}',
        "T": TOKEN,
    }
    assert credential_headers("private", server_url, env) == {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize(
    "server_url",
    [
        "https://evil.example/mcp",
        "http://tools.example/mcp",
        "https://tools.example:8443/mcp",
        "https://tools.example/mcp-other",
        "https://tools.example/",
        "https://tools.example.evil.example/mcp",
        "https://tools.example@evil.example/mcp",
    ],
)
def test_a_credential_is_never_sent_anywhere_else(server_url: str):
    env = {
        CREDENTIALS_ENV: '{"private": {"url": "https://tools.example/mcp", "token_env": "T"}}',
        "T": TOKEN,
    }
    with pytest.raises(ApplicationError) as raised:
        credential_headers("private", server_url, env)
    assert raised.value.type == "CredentialNotAllowed"
    assert raised.value.non_retryable
    assert TOKEN not in str(raised.value)


def test_an_undeclared_or_unset_credential_is_refused():
    declared = '{"private": {"url": "https://tools.example/", "token_env": "T"}}'
    with pytest.raises(ApplicationError) as unknown:
        credential_headers("other", "https://tools.example/mcp", {CREDENTIALS_ENV: declared})
    assert unknown.value.type == "UnknownCredential"
    with pytest.raises(ApplicationError) as unset:
        credential_headers("private", "https://tools.example/mcp", {CREDENTIALS_ENV: declared})
    assert unset.value.type == "MissingCredential"


@pytest.mark.parametrize(
    "declared",
    [
        '["private"]',
        '{"private": "https://tools.example/"}',
        '{"a b": {"url": "u", "token_env": "T"}}',
    ],
)
def test_a_malformed_declaration_is_refused(declared: str):
    with pytest.raises(ValueError):
        load_credentials({CREDENTIALS_ENV: declared})


# ---------------------------------------------------------------- against a server that checks


@asynccontextmanager
async def _private_mcp_server():
    """An MCP server that answers 401 unless the request carries the token."""
    app = mcp.streamable_http_app(stateless_http=True)

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != f"Bearer {TOKEN}".encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(guarded, log_level="error"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        finally:
            server.should_exit = True
            await task


async def test_the_activities_send_the_credential_to_the_server(monkeypatch: pytest.MonkeyPatch):
    async with _private_mcp_server() as url:
        _declare(monkeypatch, url)
        env = ActivityEnvironment()

        tools = await env.run(fetch_external_tools, "private", url, "private")
        assert [tool["name"] for tool in tools] == ["private_whoami"]
        called = await env.run(
            mcp_proxy_activity,
            ExternalMCPCallInput(
                server_url=url, tool_name="whoami", arguments={}, credential="private"
            ),
        )
        result = called.result.additional_properties
        assert result["content"][0]["text"] == "an authorized caller"

        with pytest.raises(Exception):
            await env.run(fetch_external_tools, "private", url)
        _declare(monkeypatch, url, token="wrong")
        with pytest.raises(Exception):
            await env.run(fetch_external_tools, "private", url, "private")


# ---------------------------------------------------------------- the registry keeps the name


async def test_the_registry_keeps_the_credential_name_and_never_the_token():
    fetched: list[tuple[str, str, str | None]] = []

    @activity.defn(name="fetch_external_tools")
    async def fake_fetch(name: str, url: str, credential: str | None = None) -> list[dict]:
        fetched.append((name, url, credential))
        return []

    task_queue = f"registry-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ToolRegistryWorkflow],
            activities=[fake_fetch],
        ):
            registry = await env.client.start_workflow(
                ToolRegistryWorkflow.run, id=f"registry-{uuid.uuid4()}", task_queue=task_queue
            )
            register = ToolRegistryWorkflow.register_external
            await registry.signal(register, args=["agent", "private", "https://a/mcp", "private"])
            await registry.signal(register, args=["agent", "open", "https://b/mcp"])

            find = ToolRegistryWorkflow.find_server
            assert await registry.query(find, args=["agent", "private"]) == RemoteServer(
                url="https://a/mcp", credential="private"
            )
            assert await registry.query(find, args=["agent", "open"]) == RemoteServer(
                url="https://b/mcp"
            )
            assert await registry.query(find, args=["agent", "missing"]) is None
            entries = await registry.query(ToolRegistryWorkflow.list_agent_entries, "agent")
            assert entries.credentials == {"private": "private"}
            assert await registry.query(ToolRegistryWorkflow.find, args=["agent", "private"]) == (
                "https://a/mcp"
            )

            # Registering the alias again without a credential removes the old one.
            await registry.signal(register, args=["agent", "private", "https://a/mcp"])
            again = await registry.query(find, args=["agent", "private"])
            assert again == RemoteServer(url="https://a/mcp")
            await registry.cancel()

    assert ("private", "https://a/mcp", "private") in fetched
    assert ("open", "https://b/mcp", None) in fetched


def _handler_client(server: RemoteServer | None = None) -> MagicMock:
    client = MagicMock(spec=nexus.TemporalNexusClient)
    handle = MagicMock()
    handle.signal = AsyncMock()
    handle.query = AsyncMock(return_value=server)
    client.client.get_workflow_handle.return_value = handle
    client.start_activity = AsyncMock(return_value=nexus.TemporalOperationResult.sync(None))
    return client


async def test_the_handler_passes_the_credential_name_to_the_registry_and_the_activity():
    handler = RegistryServiceHandler()

    client = _handler_client()
    await handler.register_external(
        MagicMock(request_id="r-1"),
        client,
        RegisterExternalInput(
            agent_id="agent", name="notes", url="https://a/mcp", credential="private"
        ),
    )
    signal = client.client.get_workflow_handle.return_value.signal
    assert signal.await_args.kwargs["args"] == ["agent", "notes", "https://a/mcp", "private"]

    client = _handler_client(RemoteServer(url="https://a/mcp", credential="private"))
    await handler.call_tool(
        MagicMock(request_id="c-1"),
        client,
        CallToolInput(agent_id="agent", alias="notes", name="notes_search"),
    )
    sent = client.start_activity.await_args.args[1]
    assert (sent.server_url, sent.tool_name) == ("https://a/mcp", "search")
    assert sent.credential == "private"


async def test_the_handler_refuses_a_credential_name_that_is_not_a_plain_name():
    with pytest.raises(nexusrpc.HandlerError) as raised:
        await RegistryServiceHandler().register_external(
            MagicMock(request_id="r-2"),
            _handler_client(),
            RegisterExternalInput(
                agent_id="agent", name="notes", url="https://a/mcp", credential="a b"
            ),
        )
    assert raised.value.type == nexusrpc.HandlerErrorType.BAD_REQUEST
