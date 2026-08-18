# ABOUTME: End-to-end tests for the session manager's create_session / track_session updates and
# the HTTP endpoints over them, run against the Temporal time-skipping test server (the only
# faithful way — the manager's updates, the tracked agent workflow, and the tool approvals the
# status/approve/attach endpoints drive all need a real workflow to execute).
#
# Run with: uv run pytest tests/web/test_session_manager.py -v

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from temporalio import workflow
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent import ToolApprovalPolicy
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
    SEND_AGENT_MESSAGE_UPDATE,
    TextMessage,
    TextReply,
)
from temporal_agent_harness.web import (
    AgentDescriptor,
    AgentRegistry,
    CreateSessionRequest,
    Session,
    SessionManagerWorkflow,
    TrackSessionRequest,
    create_agent_harness_app,
)


@agent.activity_tool_defn()
async def _gated_tool(text: str) -> str:
    return f"ran:{text}"


@workflow.defn(name="SessionManagerProbeAgent")
@agent.defn
class _SessionManagerProbeAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.always_require_approvals(),
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def act(self, message: TextMessage) -> TextReply:
        """Run the gated tool on the message text."""
        result = await self._runner.run_tool("g1", _gated_tool, message.text)
        return TextReply(text=result)


@pytest_asyncio.fixture
async def session_manager_env():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"session-manager-test-{uuid.uuid4()}"
    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="probe",
                workflow_type="SessionManagerProbeAgent",
                task_queue=task_queue,
                label="Probe",
                description="Probe agent for session-manager tests.",
            ),
            AgentDescriptor(
                key="probe-alt",
                workflow_type="AltProbeAgent",
                task_queue=task_queue,
                label="Probe Alt",
                description="A second registered agent type, never actually started.",
            ),
        ]
    )
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SessionManagerWorkflow, _SessionManagerProbeAgent],
        activities=[agent.tool_activity(_gated_tool)],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        manager_handle = await env.client.start_workflow(
            SessionManagerWorkflow.run,
            registry,
            id=f"session-manager-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        try:
            yield env.client, task_queue, registry, manager_handle, env
        finally:
            await env.shutdown()


async def _start_probe(client: Client, task_queue: str) -> str:
    workflow_id = f"externally-started-{uuid.uuid4()}"
    await client.start_workflow(
        _SessionManagerProbeAgent.run,
        AgentConfig(),
        id=workflow_id,
        task_queue=task_queue,
    )
    return workflow_id


def _wire_app(registry: AgentRegistry, client: Client, manager_handle) -> FastAPI:
    """Builds the real app, skipping its lifespan, which would connect its own Temporal client
    from env/config instead of using the time-skipping test client."""
    app = create_agent_harness_app(registry=registry)
    app.state.temporal = client
    app.state.manager_handle = manager_handle
    return app


async def _stream_until_turn_end(http: httpx.AsyncClient, workflow_id: str) -> list[dict]:
    events: list[dict] = []
    async with http.stream(
        "GET", "/api/attach", params={"session_id": workflow_id, "from_offset": 0}
    ) as response:
        assert response.status_code == 200
        async with asyncio.timeout(30):
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[len("data: ") :])
                events.append(data)
                if data["type"] == "turn_end":
                    break
    return events


async def test_track_session_appears_in_list_sessions(session_manager_env) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env
    workflow_id = "never-started-1"

    session = await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id=workflow_id, agent_workflow_type="SessionManagerProbeAgent"
        ),
        result_type=Session,
    )

    assert session.workflow_id == workflow_id
    assert session.agent_workflow_type == "SessionManagerProbeAgent"
    assert session.label == "Session 1"

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert [s.workflow_id for s in sessions] == [workflow_id]


async def test_track_session_is_callable_by_update_name_with_a_dict_payload(
    session_manager_env,
) -> None:
    """The intended caller is an activity with no harness imports, so it names the update and the
    request fields as strings instead of using the typed references the other tests do."""
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env

    session = await manager_handle.execute_update(
        "track_session",
        {
            "workflow_id": "never-started-1",
            "agent_workflow_type": "SessionManagerProbeAgent",
        },
    )

    assert session["workflow_id"] == "never-started-1"
    assert session["agent_workflow_type"] == "SessionManagerProbeAgent"

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert [s.workflow_id for s in sessions] == ["never-started-1"]


async def test_track_session_returns_existing_session_unchanged_on_repeat_call(
    session_manager_env,
) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env
    workflow_id = "never-started-1"

    first = await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id=workflow_id,
            agent_workflow_type="SessionManagerProbeAgent",
            is_message_queuing_enabled=True,
            created_at=1000.0,
        ),
        result_type=Session,
    )
    second = await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id=workflow_id,
            agent_workflow_type="AltProbeAgent",
            is_message_queuing_enabled=False,
        ),
        result_type=Session,
    )

    assert first.created_at == 1000.0
    assert second == first

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert sessions == [first]


async def test_track_session_labels_and_dedupes_each_workflow_separately(
    session_manager_env,
) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env

    async def track(workflow_id: str, queuing: bool = False) -> Session:
        return await manager_handle.execute_update(
            SessionManagerWorkflow.track_session,
            TrackSessionRequest(
                workflow_id=workflow_id,
                agent_workflow_type="SessionManagerProbeAgent",
                is_message_queuing_enabled=queuing,
            ),
            result_type=Session,
        )

    first = await track("never-started-1", queuing=True)
    second = await track("never-started-2")

    assert (first.label, second.label) == ("Session 1", "Session 2")
    assert first.is_message_queuing_enabled is True
    assert second.is_message_queuing_enabled is False
    assert await track("never-started-1") == first
    assert await track("never-started-2") == second

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert sessions == [first, second]


async def test_track_session_created_at_defaults_to_the_time_of_the_call(
    session_manager_env,
) -> None:
    _client, _task_queue, _registry, manager_handle, env = session_manager_env
    # Skipping the test server's clock on both sides of the call brackets the fallback in a window
    # minutes wide, which the wall clock, the manager's own start time, and a value in milliseconds
    # all fall well outside of.
    await env.sleep(timedelta(minutes=5))
    before = (await env.get_current_time()).timestamp()
    await env.sleep(timedelta(minutes=5))

    defaulted = await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id="never-started-1",
            agent_workflow_type="SessionManagerProbeAgent",
        ),
        result_type=Session,
    )

    await env.sleep(timedelta(minutes=5))
    after = (await env.get_current_time()).timestamp()

    explicit_zero = await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id="never-started-2",
            agent_workflow_type="SessionManagerProbeAgent",
            created_at=0.0,
        ),
        result_type=Session,
    )

    assert before < defaulted.created_at < after
    assert explicit_zero.created_at == 0.0


@pytest.mark.parametrize("created_at", [float("nan"), float("inf")])
async def test_track_session_rejects_a_non_finite_created_at(
    session_manager_env, created_at: float
) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await manager_handle.execute_update(
            SessionManagerWorkflow.track_session,
            TrackSessionRequest(
                workflow_id="never-started-1",
                agent_workflow_type="SessionManagerProbeAgent",
                created_at=created_at,
            ),
            result_type=Session,
        )
    assert getattr(excinfo.value.cause, "type", None) == "InvalidCreatedAt"
    assert getattr(excinfo.value.cause, "non_retryable", None) is True

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert sessions == []


async def test_track_session_rejects_a_non_finite_created_at_for_a_tracked_workflow_id(
    session_manager_env,
) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env
    workflow_id = "never-started-1"
    tracked = await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id=workflow_id, agent_workflow_type="SessionManagerProbeAgent"
        ),
        result_type=Session,
    )

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await manager_handle.execute_update(
            SessionManagerWorkflow.track_session,
            TrackSessionRequest(
                workflow_id=workflow_id,
                agent_workflow_type="SessionManagerProbeAgent",
                created_at=float("nan"),
            ),
            result_type=Session,
        )
    assert getattr(excinfo.value.cause, "type", None) == "InvalidCreatedAt"
    assert getattr(excinfo.value.cause, "non_retryable", None) is True

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert sessions == [tracked]


async def test_track_session_rejects_unknown_agent_type_for_a_tracked_workflow_id(
    session_manager_env,
) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env
    workflow_id = "never-started-1"
    await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id=workflow_id, agent_workflow_type="SessionManagerProbeAgent"
        ),
        result_type=Session,
    )

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await manager_handle.execute_update(
            SessionManagerWorkflow.track_session,
            TrackSessionRequest(
                workflow_id=workflow_id, agent_workflow_type="NotARealAgent"
            ),
            result_type=Session,
        )
    assert getattr(excinfo.value.cause, "type", None) == "UnknownAgentType"


async def test_track_session_rejects_unknown_agent_type(session_manager_env) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await manager_handle.execute_update(
            SessionManagerWorkflow.track_session,
            TrackSessionRequest(
                workflow_id="does-not-matter", agent_workflow_type="NotARealAgent"
            ),
            result_type=Session,
        )
    assert getattr(excinfo.value.cause, "type", None) == "UnknownAgentType"

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert sessions == []


async def test_create_session_rejects_unknown_agent_type(session_manager_env) -> None:
    _client, _task_queue, _registry, manager_handle, _env = session_manager_env

    with pytest.raises(WorkflowUpdateFailedError) as excinfo:
        await manager_handle.execute_update(
            SessionManagerWorkflow.create_session,
            CreateSessionRequest(
                agent_workflow_type="NotARealAgent", config=AgentConfig()
            ),
            result_type=Session,
        )
    assert getattr(excinfo.value.cause, "type", None) == "UnknownAgentType"
    assert getattr(excinfo.value.cause, "non_retryable", None) is True

    sessions = await manager_handle.query(
        SessionManagerWorkflow.list_sessions, result_type=list[Session]
    )
    assert sessions == []


async def test_sessions_endpoint_lists_a_tracked_session(session_manager_env) -> None:
    client, task_queue, registry, manager_handle, _env = session_manager_env
    workflow_id = await _start_probe(client, task_queue)
    await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id=workflow_id, agent_workflow_type="SessionManagerProbeAgent"
        ),
        result_type=Session,
    )

    app = _wire_app(registry, client, manager_handle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.get("/api/sessions")

    assert response.status_code == 200
    sessions = response.json()
    assert [s["workflow_id"] for s in sessions] == [workflow_id]
    assert sessions[0]["execution_status"] == "RUNNING"
    assert sessions[0]["closed"] is False


async def test_created_session_appears_in_sessions_endpoint(session_manager_env) -> None:
    client, _task_queue, registry, manager_handle, _env = session_manager_env
    app = _wire_app(registry, client, manager_handle)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        create = await http.post(
            "/api/sessions", json={"agent_workflow_type": "SessionManagerProbeAgent"}
        )
        assert create.status_code == 200
        created = create.json()

        list_response = await http.get("/api/sessions")

    assert list_response.status_code == 200
    assert [s["workflow_id"] for s in list_response.json()] == [created["workflow_id"]]


async def test_status_approve_and_attach_endpoints_work_against_a_tracked_session(
    session_manager_env,
) -> None:
    client, task_queue, registry, manager_handle, _env = session_manager_env
    workflow_id = await _start_probe(client, task_queue)
    await manager_handle.execute_update(
        SessionManagerWorkflow.track_session,
        TrackSessionRequest(
            workflow_id=workflow_id, agent_workflow_type="SessionManagerProbeAgent"
        ),
        result_type=Session,
    )

    handle = client.get_workflow_handle(workflow_id)
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="act", payload={"text": "S"}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    app = _wire_app(registry, client, manager_handle)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        listing = await http.get("/api/sessions")
        assert listing.status_code == 200
        (session_id,) = [s["workflow_id"] for s in listing.json()]

        status = await http.get(f"/api/status/{session_id}")
        assert status.status_code == 200
        assert [a["tool_id"] for a in status.json()["pending_approvals"]] == ["g1"]

        approve = await http.post(
            "/api/approve",
            json={"session_id": session_id, "tool_id": "g1", "approved": True},
        )
        assert approve.status_code == 200
        assert approve.json() == {"tool_id": "g1", "accepted": True}

        events = await _stream_until_turn_end(http, session_id)

    tool_events = [e["type"] for e in events if e.get("tool_id") == "g1"]
    assert tool_events == [
        "tool_approval_requested",
        "tool_approval_resolved",
        "tool_start",
        "tool_end",
    ]
