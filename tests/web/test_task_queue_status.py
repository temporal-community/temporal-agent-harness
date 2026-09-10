from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.protobuf.timestamp_pb2 import Timestamp
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import DescribeTaskQueueResponse

from temporal_agent_harness.web import (
    AgentDescriptor,
    AgentRegistry,
    create_agent_harness_app,
)
from temporal_agent_harness.web.task_queue_status import (
    NO_WORKER,
    READY,
    UNKNOWN,
    describe_task_queue_workers,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _poller(identity: str, *, age: timedelta, base: datetime = NOW) -> PollerInfo:
    """A poller last seen ``age`` ago. ``base`` defaults to the frozen ``NOW`` the unit tests
    pass through to the freshness check; the endpoint tests go through the real clock, so
    they anchor to it instead."""
    last_access = Timestamp()
    last_access.FromDatetime(base - age)
    return PollerInfo(identity=identity, last_access_time=last_access)


def _live_poller(identity: str) -> PollerInfo:
    return _poller(identity, age=timedelta(seconds=5), base=datetime.now(timezone.utc))


class _FakeWorkflowService:
    def __init__(self, responses: dict[str, DescribeTaskQueueResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[object] = []

    async def describe_task_queue(self, request, timeout=None):
        self.requests.append(request)
        response = self.responses[request.task_queue.name]
        if isinstance(response, Exception):
            raise response
        return response


class _FakeTemporalClient:
    def __init__(self, responses: dict[str, DescribeTaskQueueResponse | Exception]) -> None:
        self.namespace = "default"
        self.workflow_service = _FakeWorkflowService(responses)


async def test_fresh_poller_reports_ready_with_count_and_last_seen() -> None:
    temporal = _FakeTemporalClient(
        {
            "monty-dynamic-agent": DescribeTaskQueueResponse(
                pollers=[
                    _poller("worker-a", age=timedelta(seconds=45)),
                    _poller("worker-b", age=timedelta(seconds=5)),
                ]
            )
        }
    )

    workers = await describe_task_queue_workers(
        temporal, ["monty-dynamic-agent"], now=NOW
    )

    status = workers.for_task_queue("monty-dynamic-agent")
    assert status.status == READY
    assert status.poller_count == 2
    # The most recent poll across live pollers, not the oldest.
    assert status.last_seen == (NOW - timedelta(seconds=5)).timestamp()
    assert status.error is None


async def test_queue_with_no_pollers_reports_no_worker() -> None:
    temporal = _FakeTemporalClient({"react-agent": DescribeTaskQueueResponse(pollers=[])})

    workers = await describe_task_queue_workers(temporal, ["react-agent"], now=NOW)

    assert workers.for_task_queue("react-agent").status == NO_WORKER
    assert workers.for_task_queue("react-agent").poller_count == 0


async def test_poller_older_than_freshness_window_is_not_a_live_worker() -> None:
    """The server keeps poller entries for minutes after a worker dies, so presence alone
    would keep the picker claiming Ready at a queue nobody is listening to."""

    temporal = _FakeTemporalClient(
        {
            "openai-hello": DescribeTaskQueueResponse(
                pollers=[_poller("dead-worker", age=timedelta(minutes=4))]
            )
        }
    )

    workers = await describe_task_queue_workers(temporal, ["openai-hello"], now=NOW)

    assert workers.for_task_queue("openai-hello").status == NO_WORKER


async def test_mixed_fresh_and_stale_pollers_count_only_the_fresh() -> None:
    temporal = _FakeTemporalClient(
        {
            "nexus-hello": DescribeTaskQueueResponse(
                pollers=[
                    _poller("dead-worker", age=timedelta(minutes=4)),
                    _poller("live-worker", age=timedelta(seconds=10)),
                ]
            )
        }
    )

    workers = await describe_task_queue_workers(temporal, ["nexus-hello"], now=NOW)

    status = workers.for_task_queue("nexus-hello")
    assert status.status == READY
    assert status.poller_count == 1


async def test_shared_task_queue_is_described_once_for_all_its_agents() -> None:
    """Monty's two conversational agents share ``monty-dynamic-agent`` — the supported
    pattern, since one worker registers both — so the picker must not pay an RPC per agent."""

    temporal = _FakeTemporalClient(
        {
            "monty-dynamic-agent": DescribeTaskQueueResponse(
                pollers=[_poller("worker-a", age=timedelta(seconds=5))]
            )
        }
    )

    workers = await describe_task_queue_workers(
        temporal,
        ["monty-dynamic-agent", "monty-dynamic-agent", "monty-dynamic-agent"],
        now=NOW,
    )

    assert len(temporal.workflow_service.requests) == 1
    assert workers.for_task_queue("monty-dynamic-agent").status == READY


async def test_describes_workflow_pollers_for_the_clients_namespace() -> None:
    temporal = _FakeTemporalClient({"react-agent": DescribeTaskQueueResponse(pollers=[])})

    await describe_task_queue_workers(temporal, ["react-agent"], now=NOW)

    request = temporal.workflow_service.requests[0]
    assert request.namespace == "default"
    assert request.task_queue.name == "react-agent"
    assert request.task_queue_type == TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW


async def test_a_failed_describe_reports_unknown_rather_than_failing_the_list() -> None:
    """One unreachable queue must not take down the whole agent list — the registry is still
    worth serving, and the readiness column admits it does not know."""

    temporal = _FakeTemporalClient(
        {
            "monty-dynamic-agent": DescribeTaskQueueResponse(
                pollers=[_poller("worker-a", age=timedelta(seconds=5))]
            ),
            "react-agent": RuntimeError("deadline exceeded"),
        }
    )

    workers = await describe_task_queue_workers(
        temporal, ["monty-dynamic-agent", "react-agent"], now=NOW
    )

    assert workers.for_task_queue("monty-dynamic-agent").status == READY
    broken = workers.for_task_queue("react-agent")
    assert broken.status == UNKNOWN
    assert "deadline exceeded" in (broken.error or "")


async def test_an_undescribed_queue_is_unknown_not_ready() -> None:
    workers = await describe_task_queue_workers(_FakeTemporalClient({}), [], now=NOW)

    assert workers.for_task_queue("never-asked").status == UNKNOWN


class _FakeManagerHandle:
    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry

    async def query(self, _query, **_kwargs) -> AgentRegistry:
        return self.registry


def _agents_app(registry: AgentRegistry, temporal: _FakeTemporalClient) -> FastAPI:
    """Build the app and hand it its dependencies directly.

    ``TestClient`` only runs the lifespan inside a ``with`` block, and the lifespan would
    connect to a real Temporal server — so the endpoint gets the same two pieces of state
    the lifespan would have set.
    """
    app = create_agent_harness_app(registry=registry)
    app.state.temporal = temporal
    app.state.manager_handle = _FakeManagerHandle(registry)
    return app


def test_api_agents_reports_worker_readiness_per_agent() -> None:
    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="monty-chat",
                workflow_type="MontyChatAgent",
                task_queue="monty-dynamic-agent",
                label="Monty (Conversational)",
                description="Chats.",
            ),
            AgentDescriptor(
                key="react",
                workflow_type="ReactAgent",
                task_queue="react-agent",
                label="React",
                description="Reasons.",
            ),
        ]
    )
    temporal = _FakeTemporalClient(
        {
            "monty-dynamic-agent": DescribeTaskQueueResponse(
                pollers=[_live_poller("worker-a")]
            ),
            "react-agent": DescribeTaskQueueResponse(pollers=[]),
        }
    )

    response = TestClient(_agents_app(registry, temporal)).get("/api/agents")

    assert response.status_code == 200
    agents = {agent["key"]: agent for agent in response.json()["agents"]}
    assert agents["monty-chat"]["worker"]["status"] == READY
    assert agents["monty-chat"]["worker"]["poller_count"] == 1
    assert agents["react"]["worker"]["status"] == NO_WORKER
    # The descriptor fields the UI already renders are untouched.
    assert agents["react"]["label"] == "React"
    assert agents["react"]["task_queue"] == "react-agent"


def test_api_agents_is_never_cached() -> None:
    """Readiness is live: a worker can come up or go down between two opens of the picker,
    and a cached "Ready" is the exact lie this endpoint exists to stop telling."""

    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="react",
                workflow_type="ReactAgent",
                task_queue="react-agent",
                label="React",
                description="Reasons.",
            )
        ]
    )
    temporal = _FakeTemporalClient({"react-agent": DescribeTaskQueueResponse(pollers=[])})

    response = TestClient(_agents_app(registry, temporal)).get("/api/agents")

    assert response.headers["cache-control"] == "no-store"


async def test_freshness_window_is_configurable() -> None:
    temporal = _FakeTemporalClient(
        {
            "react-agent": DescribeTaskQueueResponse(
                pollers=[_poller("worker-a", age=timedelta(seconds=90))]
            )
        }
    )

    generous = await describe_task_queue_workers(
        temporal, ["react-agent"], freshness=timedelta(minutes=5), now=NOW
    )
    strict = await describe_task_queue_workers(
        temporal, ["react-agent"], freshness=timedelta(seconds=30), now=NOW
    )

    assert generous.for_task_queue("react-agent").status == READY
    assert strict.for_task_queue("react-agent").status == NO_WORKER
