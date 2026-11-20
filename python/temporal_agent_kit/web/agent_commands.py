"""``temporal-agent-kit agents``: register, list, retire and delete a running server's agents.

Each command is one call to the server's HTTP API, the same one the console uses, so the server
applies its registry's rules and its ``TASK_QUEUE_PREFIX`` to the CLI's requests as to anyone's.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import JsonValue

from temporal_agent_kit.web.registry import load_registrations

DEFAULT_SERVER = "http://localhost:8000"
_TIMEOUT_SECONDS = 30


class AgentCommandError(Exception):
    """A request the server refused or could not be reached for."""


def add_agents(path: Path, server: str = DEFAULT_SERVER) -> list[str]:
    """Register every agent in the ``agents.toml`` at ``path``, all or none, and return their
    keys."""
    registrations = load_registrations(path)
    body = {
        "agents": [
            registration.model_dump(mode="json", by_alias=True) for registration in registrations
        ]
    }
    reply = _request(server, "POST", "/api/agents", body)
    return [agent["key"] for agent in _agents(reply)]


def list_agents(server: str = DEFAULT_SERVER) -> list[dict[str, JsonValue]]:
    """Every agent the server's registry holds, retired ones included."""
    return _agents(_request(server, "GET", "/api/agents"))


def retire_agent(key: str, server: str = DEFAULT_SERVER) -> None:
    _request(server, "POST", f"/api/agents/{quote(key, safe='')}/retire")


def delete_agent(key: str, server: str = DEFAULT_SERVER) -> None:
    _request(server, "DELETE", f"/api/agents/{quote(key, safe='')}")


def format_agents(agents: list[dict[str, JsonValue]]) -> str:
    """One line per agent: its key, state, workflow type and task queue."""
    if not agents:
        return "No agents are registered."
    rows = [
        (
            str(agent["key"]),
            "retired" if agent.get("retired") else "active",
            str(agent["workflow_type"]),
            str(agent["task_queue"]),
        )
        for agent in agents
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    return "\n".join(
        "  ".join([*(cell.ljust(width) for cell, width in zip(row, widths)), row[3]])
        for row in rows
    )


def _agents(reply: JsonValue) -> list[dict[str, JsonValue]]:
    if not isinstance(reply, dict) or not isinstance(reply.get("agents"), list):
        raise AgentCommandError(f"The server's reply is not an agent list: {reply!r}")
    return [agent for agent in reply["agents"] if isinstance(agent, dict)]


def _request(
    server: str, method: str, path: str, body: dict[str, JsonValue] | None = None
) -> JsonValue:
    url = server.rstrip("/") + path
    request = Request(
        url,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return json.loads(response.read() or b"null")
    except HTTPError as e:
        raise AgentCommandError(f"{method} {url} -> {e.code}: {_message(e)}") from None
    except URLError as e:
        raise AgentCommandError(
            f"Cannot reach the Agent Kit server at {server} ({e.reason}). Is "
            "`temporal-agent-kit serve` running there?"
        ) from None


def _message(error: HTTPError) -> str:
    text = error.read().decode(errors="replace")
    try:
        content = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(content, dict):
        message = content.get("message", content.get("detail"))
        if message is not None:
            return message if isinstance(message, str) else json.dumps(message)
    return text
