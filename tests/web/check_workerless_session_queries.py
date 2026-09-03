# ABOUTME: Asserts that a session whose task queue has no worker produces NO query to that
# workflow — the consequence, not the decision. A query to a workerless workflow is not a
# failed request, it is a slot held in Temporal's per-workflow query buffer for the whole
# query timeout while the SDK retries it internally; fill that buffer and Temporal sheds
# everything else touching the workflow as RESOURCE_EXHAUSTED/BusyWorkflow. So "did we send
# one" is the only thing worth asserting: a check that the skip *decision* was taken would
# pass just as happily while the query went out on some path nobody thought to route.
#     uv run python tests/web/check_workerless_session_queries.py
#
# Drives the real app from create_agent_harness_app, the real _WorkerPresence cache and the
# real endpoint bodies. Only Temporal is stood in for, because Temporal is the thing being
# counted: the stub records every query_workflow it is asked for and answers
# describe_task_queue with a poller list, which is exactly the pair the fix trades between.
#
# Teeth: delete `await _require_worker(session_id)` from the /api/agent-interface,
# /api/status or /api/operator-interface handler in temporal_agent_harness/web/app.py and
# the first assertion fails, reporting the queries that were sent.

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app
from temporal_agent_harness.web.session_manager import AgentDescriptor, Session

SERVED_QUEUE = "served-agent"
WORKERLESS_QUEUE = "workerless-agent"

SERVED_SESSION = "agent-session-served"
WORKERLESS_SESSION = "agent-session-workerless"

TASK_QUEUES = {SERVED_SESSION: SERVED_QUEUE, WORKERLESS_SESSION: WORKERLESS_QUEUE}
POLLER_COUNTS = {SERVED_QUEUE: 1, WORKERLESS_QUEUE: 0}

REGISTRY = AgentRegistry(
    agents=[
        AgentDescriptor(
            key="served",
            workflow_type="ServedAgent",
            task_queue=SERVED_QUEUE,
            label="Served",
            description="",
        )
    ]
)


@dataclass
class _Recorder:
    """Every RPC the app sent, so the check can assert on what left the process."""

    queries: list[str] = field(default_factory=list)
    described_queues: list[str] = field(default_factory=list)


class _Pollers:
    def __init__(self, count: int) -> None:
        self.pollers = [object()] * count


class _Description:
    def __init__(self, task_queue: str) -> None:
        self.task_queue = task_queue
        self.status = _RunningStatus()

    async def memo(self) -> dict[str, Any]:
        return {}


class _RunningStatus:
    name = "RUNNING"

    def __ne__(self, other: object) -> bool:
        return True  # never equal to WorkflowExecutionStatus.RUNNING's identity check

    def __eq__(self, other: object) -> bool:
        return False


class _Handle:
    def __init__(self, workflow_id: str, recorder: _Recorder) -> None:
        self._workflow_id = workflow_id
        self._recorder = recorder

    async def describe(self) -> _Description:
        task_queue = TASK_QUEUES.get(self._workflow_id)
        if task_queue is None:
            raise RPCError("no such workflow", RPCStatusCode.NOT_FOUND, b"")
        return _Description(task_queue)

    async def query(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        """What the fix exists to prevent for a workerless queue.

        Answers as the real thing does: a served queue returns, a workerless one is shed by
        Temporal after occupying the buffer. Recorded either way — being shed is not the
        harm, having asked at all is.
        """
        self._recorder.queries.append(self._workflow_id)
        if POLLER_COUNTS[TASK_QUEUES[self._workflow_id]] == 0:
            raise RPCError(
                "consistent query buffer is full",
                RPCStatusCode.RESOURCE_EXHAUSTED,
                b"",
            )
        return []


class _WorkflowService:
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    async def describe_task_queue(self, request: Any) -> _Pollers:
        name = request.task_queue.name
        self._recorder.described_queues.append(name)
        return _Pollers(POLLER_COUNTS.get(name, 0))


class _Temporal:
    namespace = "check"

    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self.workflow_service = _WorkflowService(recorder)

    def get_workflow_handle(self, workflow_id: str, **_kwargs: Any) -> _Handle:
        return _Handle(workflow_id, self._recorder)


class _Manager:
    async def query(self, _name: Any, result_type: Any = None) -> Any:
        if result_type is AgentRegistry:
            return REGISTRY
        return [
            Session(
                workflow_id=workflow_id,
                created_at=0.0,
                label=workflow_id,
                agent_workflow_type="ServedAgent",
            )
            for workflow_id in (SERVED_SESSION, WORKERLESS_SESSION)
        ]


def _build() -> tuple[TestClient, _Recorder]:
    recorder = _Recorder()
    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.temporal = _Temporal(recorder)
    app.state.manager_handle = _Manager()
    return TestClient(app, raise_server_exceptions=False), recorder


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


QUERYING_ENDPOINTS = ("agent-interface", "operator-interface", "status")


def main() -> None:
    # --- the bug: every querying endpoint, against a session with no worker ---------------
    # Each of these used to send a query that sat in the buffer for the query timeout. The
    # assertion is on the recorder, not on the status code, because a 503 could equally be
    # produced by sending the query and translating the failure — which would leave the
    # counter climbing exactly as before.
    client, recorder = _build()
    for endpoint in QUERYING_ENDPOINTS:
        response = client.get(f"/api/{endpoint}/{WORKERLESS_SESSION}")
        if response.status_code != 503:
            _fail(
                f"/api/{endpoint} on a workerless session answered "
                f"{response.status_code}, expected 503"
            )
        body = response.json()
        if body.get("error") != "agent_worker_unavailable":
            _fail(f"/api/{endpoint} must name the cause, got {body!r}")
        if response.headers.get("Retry-After") is None:
            _fail(f"/api/{endpoint} must carry Retry-After so the client can back off")

    if recorder.queries:
        _fail(
            "a workerless session must produce NO workflow query; these were sent: "
            f"{recorder.queries}. Each one occupies Temporal's per-workflow query buffer "
            "for the query timeout and is what earns the console its BusyWorkflow "
            "RESOURCE_EXHAUSTED."
        )

    # --- the poll: repeating it must not reintroduce the queries -------------------------
    # The steady state is the whole point. A cooldown that expires into a fresh query would
    # pass the single-request case above and still climb the counter forever.
    for _ in range(12):
        client.get(f"/api/agent-interface/{WORKERLESS_SESSION}")
    if recorder.queries:
        _fail(f"repeated polls sent queries after all: {recorder.queries}")

    # --- the thing a quarantine would break ----------------------------------------------
    # A fix that skipped everything would satisfy every assertion above. A served session
    # must still be queried and must still answer.
    served = client.get(f"/api/agent-interface/{SERVED_SESSION}")
    if served.status_code != 200:
        _fail(
            f"a session WITH a worker must still answer, got {served.status_code}: "
            f"{served.text[:200]}"
        )
    if SERVED_SESSION not in recorder.queries:
        _fail("a session with a worker must still be queried, not skipped")

    # --- the list stays whole, and says which rows are unserved ---------------------------
    listed = client.get("/api/sessions")
    if listed.status_code != 200:
        _fail(f"/api/sessions must answer, got {listed.status_code}")
    rows = {row["workflow_id"]: row for row in listed.json()}
    if set(rows) != {SERVED_SESSION, WORKERLESS_SESSION}:
        _fail(f"one dead session must not cost the others; saw {sorted(rows)}")
    if rows[WORKERLESS_SESSION].get("worker_available") is not False:
        _fail(
            "the workerless session must report worker_available=false rather than "
            f"looking healthy; got {rows[WORKERLESS_SESSION].get('worker_available')!r}"
        )
    if rows[SERVED_SESSION].get("worker_available") is not True:
        _fail(
            "the served session must report worker_available=true; got "
            f"{rows[SERVED_SESSION].get('worker_available')!r}"
        )

    # --- caching: distinct queues, not one probe per session per poll ---------------------
    before = len(recorder.described_queues)
    for _ in range(5):
        client.get("/api/sessions")
    probes = len(recorder.described_queues) - before
    if probes > 5 * 2:
        _fail(
            f"the poller probe must be cached per task queue, sent {probes} probes "
            "across 5 polls of 2 queues"
        )

    print(
        "workerless session queries: ok "
        f"(0 queries to {WORKERLESS_SESSION} across {3 + 12} requests; "
        f"{len(recorder.queries)} to the served session; "
        f"{len(recorder.described_queues)} task-queue probes total)"
    )


if __name__ == "__main__":
    main()
