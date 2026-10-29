"""The Python server serving an agent written with the TypeScript SDK.

There is one server (HTTP API, session manager, console) for agents written with either SDK. Here
it serves ``BriefedTsAgent`` from //typescript/packages/worker/test/serving: the TypeScript worker
runs as a subprocess on the dev server, the registry and schema document are the committed ones
the TypeScript ``agent-harness-schema`` bin wrote, and the session goes through the same routes
the console uses.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from pydantic import JsonValue

from temporal_agent_harness.web import (
    create_agent_harness_app,
    create_session_manager_worker,
    load_agent_registry,
)
from tests.conformance.driver import external_worker

# Relative to the runfiles root, the test's working directory.
SERVING = Path("typescript/packages/worker/test/serving")
REGISTRY = SERVING / "agents.toml"
WORKER_ENV = "TYPESCRIPT_SERVING_WORKER"

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def http(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[httpx.AsyncClient]:
    """The harness app, started as ``serve`` starts it, against a dev server hosting the
    TypeScript worker on its registry task queue and the session-manager worker."""
    registry = load_agent_registry(REGISTRY)
    (agent,) = registry.agents
    command = [os.path.abspath(os.environ[WORKER_ENV])]
    async with external_worker(command, task_queue=agent.task_queue) as (client, _):
        with pytest.MonkeyPatch.context() as env:
            # The app connects from the environment, so point that at the dev server alone.
            env.setenv("TEMPORAL_CONFIG_FILE", str(tmp_path_factory.mktemp("config") / "absent.toml"))
            env.delenv("TEMPORAL_PROFILE", raising=False)
            env.setenv("TEMPORAL_ADDRESS", client.service_client.config.target_host)
            env.setenv("TEMPORAL_NAMESPACE", client.namespace)
            app = create_agent_harness_app(registry=registry)
            async with app.router.lifespan_context(app):
                async with create_session_manager_worker(app.state.temporal):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test"
                    ) as http:
                        yield http


def _sse_events(body: str) -> list[tuple[str, dict[str, JsonValue]]]:
    events: list[tuple[str, dict[str, JsonValue]]] = []
    for frame in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


async def test_agents_list_publishes_the_typescript_agents_required_brief(http) -> None:
    response = await http.get("/api/agents")

    assert response.status_code == 200, response.text
    agents = {a["key"]: a for a in response.json()["agents"]}
    init_data = agents["briefed-ts"]["init_data"]
    assert init_data["required"] is True
    assert init_data["schema"]["title"] == "Brief"


async def test_starting_without_the_required_brief_is_a_422(http) -> None:
    response = await http.post("/api/sessions", json={"agent_workflow_type": "BriefedTsAgent"})

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_init_data"


async def test_a_session_started_with_a_brief_answers_through_the_console_routes(http) -> None:
    started = await http.post(
        "/api/sessions",
        json={"agent_workflow_type": "BriefedTsAgent", "data": {"topic": "tides"}},
    )
    assert started.status_code == 200, started.text
    session_id = started.json()["workflow_id"]

    sent = await http.post(
        "/api/messages",
        json={
            "session_id": session_id,
            "message": {"type": "ask", "payload": {"text": "why?"}},
        },
    )
    assert sent.status_code == 200, sent.text
    message_id = sent.json()["message_id"]

    # attach ends once the agent is idle after a turn_end.
    attached = await http.get("/api/attach", params={"session_id": session_id})
    assert attached.status_code == 200, attached.text
    events = _sse_events(attached.text)
    assert [kind for kind, _ in events][-1] == "turn_end"
    replies = [
        data["output"]
        for kind, data in events
        if kind == "message_handler_end" and data["message_id"] == message_id
    ]
    assert replies == [{"text": "tides: why?"}]
