"""Sessions are started directly and listed from Temporal visibility.

Runs on a local dev server, since the time-skipping test server has no visibility queries. Every
test gets its own task queue, so the sessions other tests leave on the shared server are never
in its list — the same isolation ``TASK_QUEUE_PREFIX`` gives two deployments in one namespace.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

import httpx
import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_kit import AgentWorkflowRunner, agent
from temporal_agent_kit.agent_client import AgentClient
from temporal_agent_kit.agent_protocol import (
    AgentConfig,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_kit.web import AgentDescriptor, AgentRegistry, create_agent_kit_app
from temporal_agent_kit.web.sessions import (
    Session,
    get_session,
    list_sessions,
    sessions_query,
    start_session,
)

T = TypeVar("T")


@agent.defn(name="ListedAgent")
class ListedAgent:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @agent.accepts
    async def ask(self, message: TextMessage) -> TextReply:
        """Ask."""
        return TextReply(text=message.text)


@workflow.defn(name="ListedAgentParent")
class ListedAgentParent:
    """Starts a ``ListedAgent`` as its child, as an agent starts a subagent."""

    @workflow.run
    async def run(self, child_id: str, task_queue: str) -> None:
        await workflow.start_child_workflow(
            "ListedAgent", AgentConfig(), id=child_id, task_queue=task_queue
        )
        await workflow.wait_condition(lambda: False)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    env = await WorkflowEnvironment.start_local(data_converter=pydantic_data_converter)
    yield env
    await env.shutdown()


@pytest_asyncio.fixture(loop_scope="module")
async def registry(env: WorkflowEnvironment) -> AsyncIterator[AgentRegistry]:
    """A registry of ``ListedAgent`` on a queue of its own, with a worker polling it."""
    task_queue = f"listed-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ListedAgent, ListedAgentParent],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        yield AgentRegistry(
            agents=[
                AgentDescriptor(
                    key="listed",
                    workflow_type="ListedAgent",
                    task_queue=task_queue,
                    label="Listed",
                    description="Answers.",
                )
            ]
        )


pytestmark = pytest.mark.asyncio(loop_scope="module")


async def _eventually(check: Callable[[], Awaitable[T | None]], timeout: float = 5) -> T:
    """``check``'s first truthy result. Visibility trails a workflow's own state slightly."""
    async with asyncio.timeout(timeout):
        while not (result := await check()):
            await asyncio.sleep(0.05)
        return result


async def _count(client: Client, query: str) -> int:
    return (await client.count_workflows(query)).count


async def _listed_ids(client: Client, registry: AgentRegistry, count: int) -> list[str]:
    async def listed() -> list[str] | None:
        page = await list_sessions(client, registry)
        ids = [session.workflow_id for session in page.sessions]
        return ids if len(ids) >= count else None

    return await _eventually(listed)


async def test_sessions_are_listed_newest_first_a_page_at_a_time(env, registry) -> None:
    (agent_,) = registry.agents
    started = [await start_session(env.client, agent_, registry) for _ in range(3)]

    assert await _listed_ids(env.client, registry, 3) == [s.workflow_id for s in reversed(started)]

    first = await list_sessions(env.client, registry, page_size=2)
    assert len(first.sessions) == 2
    assert first.next_page_token is not None
    rest = await list_sessions(
        env.client, registry, page_size=2, page_token=first.next_page_token
    )
    assert [s.workflow_id for s in first.sessions + rest.sessions] == [
        s.workflow_id for s in reversed(started)
    ]
    assert rest.next_page_token is None


async def test_a_listed_session_carries_its_agent_and_status(env, registry) -> None:
    (agent_,) = registry.agents
    started = await start_session(env.client, agent_, registry)
    await _listed_ids(env.client, registry, 1)

    (listed,) = (await list_sessions(env.client, registry)).sessions
    assert listed == Session(
        workflow_id=started.workflow_id,
        agent_workflow_type="ListedAgent",
        label="Listed",
        created_at=started.created_at,
        execution_status="RUNNING",
        closed=False,
        title=None,
    )


async def test_the_first_message_becomes_the_sessions_title(env, registry) -> None:
    (agent_,) = registry.agents
    started = await start_session(env.client, agent_, registry)
    client = AgentClient(temporal=env.client, workflow_id=started.workflow_id)
    await client.submit_message("ask", {"text": "What is the tide doing?"})
    await client.submit_message("ask", {"text": "And tomorrow?"})

    async def titled() -> str | None:
        (session,) = (await list_sessions(env.client, registry)).sessions
        return session.title

    assert await _eventually(titled) == "What is the tide doing?"


async def test_a_closed_session_is_still_listed_as_closed(env, registry) -> None:
    (agent_,) = registry.agents
    started = await start_session(env.client, agent_, registry)
    handle = env.client.get_workflow_handle(started.workflow_id)
    await handle.signal("close")
    async with asyncio.timeout(5):
        await handle.result()

    async def closed() -> Session | None:
        sessions = (await list_sessions(env.client, registry)).sessions
        return sessions[0] if sessions and sessions[0].closed else None

    assert (await _eventually(closed)).execution_status == "COMPLETED"


async def test_subagents_and_other_queues_are_not_sessions(env, registry) -> None:
    (agent_,) = registry.agents
    session = await start_session(env.client, agent_, registry)
    child_id = f"listed-subagent-{uuid.uuid4()}"
    await env.client.start_workflow(
        ListedAgentParent.run,
        args=[child_id, agent_.task_queue],
        id=f"listed-parent-{uuid.uuid4()}",
        task_queue=agent_.task_queue,
    )
    elsewhere_id = f"elsewhere-{uuid.uuid4()}"
    await env.client.start_workflow(
        "ListedAgent",
        AgentConfig(),
        id=elsewhere_id,
        task_queue=f"{agent_.task_queue}-elsewhere",
    )

    # Both are in visibility, so leaving them out of the list is the query's doing.
    for workflow_id in (child_id, elsewhere_id):
        await _eventually(
            lambda: _count(env.client, f"WorkflowId = '{workflow_id}' AND WorkflowType = 'ListedAgent'")
        )

    assert await _listed_ids(env.client, registry, 1) == [session.workflow_id]
    assert await get_session(env.client, registry, child_id) is None


async def test_a_running_session_id_is_started_once(env, registry) -> None:
    (agent_,) = registry.agents
    chosen = f"slack:C1:{uuid.uuid4()}"

    first = await start_session(env.client, agent_, registry, session_id=chosen)
    first_run = (await env.client.get_workflow_handle(chosen).describe()).run_id
    second = await start_session(env.client, agent_, registry, session_id=chosen)

    assert first.workflow_id == second.workflow_id == chosen
    assert (await env.client.get_workflow_handle(chosen).describe()).run_id == first_run


async def test_the_query_matches_each_agent_on_its_own_queue() -> None:
    registry = AgentRegistry(
        agents=[
            AgentDescriptor("a", "AgentA", "queue-a", "A", "A."),
            AgentDescriptor("b", "Agent'B", "queue-b", "B", "B."),
        ]
    )

    assert sessions_query(registry) == (
        "((WorkflowType = 'AgentA' AND TaskQueue = 'queue-a') OR "
        "(WorkflowType = 'Agent\\'B' AND TaskQueue = 'queue-b')) AND ParentWorkflowId IS NULL"
    )


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


async def _http(env: WorkflowEnvironment, registry: AgentRegistry) -> httpx.AsyncClient:
    app = create_agent_kit_app(registry=registry)
    app.state.temporal = env.client
    app.state.registry = registry
    app.state.agent_starts = {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_the_routes_start_list_and_get_sessions(env, registry) -> None:
    async with await _http(env, registry) as http:
        created = await http.post("/api/sessions", json={"agent_workflow_type": "ListedAgent"})
        assert created.status_code == 200, created.text
        session_id = created.json()["workflow_id"]
        assert session_id.startswith("agent-session-")

        await _listed_ids(env.client, registry, 1)
        page = (await http.get("/api/sessions", params={"page_size": 10})).json()
        assert [s["workflow_id"] for s in page["sessions"]] == [session_id]
        assert page["next_page_token"] is None

        one = await http.get(f"/api/sessions/{session_id}")
        assert one.status_code == 200
        assert one.json()["label"] == "Listed"
        assert (await http.get("/api/sessions/no-such-session")).status_code == 404


async def test_the_routes_refuse_what_is_not_theirs(env, registry) -> None:
    taken = f"not-a-session-{uuid.uuid4()}"
    await env.client.start_workflow(
        "SomethingElse", id=taken, task_queue=f"something-else-{uuid.uuid4()}"
    )
    async with await _http(env, registry) as http:
        unknown = await http.post("/api/sessions", json={"agent_workflow_type": "Nobody"})
        assert unknown.status_code == 422

        clash = await http.post(
            "/api/sessions", json={"agent_workflow_type": "ListedAgent", "session_id": taken}
        )
        assert clash.status_code == 409

        assert (await http.get(f"/api/sessions/{taken}")).status_code == 404
        bad_token = await http.get("/api/sessions", params={"page_token": "!!"})
        assert bad_token.status_code == 400
