# ABOUTME: Restarting one app server used to make other servers' live sessions vanish from the
# console. On startup a server pushes the agents IT serves to the shared manager, and session
# discovery — which can only recognise an agent workflow by its type — was reading that same list,
# so a seven-agent server evicted the types a running ScheduledDigestAgent was found by. These pin
# that discovery reads the accumulated set while the create menu keeps reading the current one.

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from temporalio.client import WorkflowQueryFailedError

from temporal_agent_harness.web import create_agent_harness_app
from temporal_agent_harness.web.session_manager import (
    AgentDescriptor,
    AgentRegistry,
    Session,
    SessionManagerWorkflow,
)

RUNNING_DIGEST = "scheduled-digest.dispatch-2026-09-02T09:00:00Z"


def _descriptor(workflow_type: str) -> AgentDescriptor:
    return AgentDescriptor(
        key=workflow_type.lower(),
        workflow_type=workflow_type,
        task_queue=workflow_type.lower(),
        label=workflow_type,
        description="",
    )


# What the server that had been up was serving, and what the one that restarted serves.
WIDE = AgentRegistry(agents=[_descriptor("ScheduledDigestAgent"), _descriptor("HelloAgent")])
NARROW = AgentRegistry(agents=[_descriptor("HelloAgent")])


def test_a_push_that_omits_an_agent_still_leaves_it_discoverable() -> None:
    # The handlers touch nothing but their own state, so this is the manager itself, not a
    # re-implementation of it.
    manager = SessionManagerWorkflow(WIDE)
    manager.set_available_agents(NARROW)

    offered = {agent.workflow_type for agent in manager.available_agents().agents}
    discoverable = {agent.workflow_type for agent in manager.discoverable_agents().agents}

    # Withdrawn from the create menu, which is what the push legitimately means...
    assert offered == {"HelloAgent"}
    # ...and still recognisable as a running session, which it never meant.
    assert discoverable == {"ScheduledDigestAgent", "HelloAgent"}


def test_the_latest_push_wins_on_a_type_it_shares() -> None:
    # Accumulating must not pin an agent to the task queue it was first announced on, or a
    # re-queued agent would be discovered under a stale label forever.
    manager = SessionManagerWorkflow(WIDE)
    moved = AgentDescriptor(
        key="hello",
        workflow_type="HelloAgent",
        task_queue="hello-v2",
        label="Hello v2",
        description="",
    )
    manager.set_available_agents(AgentRegistry(agents=[moved]))

    assert manager.discoverable_agents().by_workflow_type("HelloAgent") == moved


TRACKED = "agent-session-tracked"


class _Manager:
    """The session manager as the endpoint sees it: narrowed offering, wider memory."""

    def __init__(self, *, knows_discoverable: bool = True) -> None:
        self.asked: list[str] = []
        self._knows_discoverable = knows_discoverable

    async def query(self, name, result_type=None):
        self.asked.append(name.__name__)
        if name.__name__ == "list_sessions":
            return [
                Session(
                    workflow_id=TRACKED,
                    created_at=0.0,
                    label="Session 1",
                    agent_workflow_type="HelloAgent",
                )
            ]
        if name.__name__ == "available_agents":
            return NARROW
        if not self._knows_discoverable:
            raise WorkflowQueryFailedError("unknown queryType discoverable_agents")
        return WIDE


class _Temporal:
    def list_workflows(self, query: str, limit: int | None = None):
        async def running():
            if "ScheduledDigestAgent" not in query:
                return
            yield SimpleNamespace(
                id=RUNNING_DIGEST,
                workflow_type="ScheduledDigestAgent",
                start_time=datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
            )

        return running()


async def _state(_temporal, session: Session):
    return {**session.__dict__, "execution_status": "RUNNING", "closed": False}


def _sessions(manager: _Manager):
    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.manager_handle = manager
    app.state.temporal = _Temporal()

    with patch("temporal_agent_harness.web.app._session_with_execution_state", _state):
        response = TestClient(app, raise_server_exceptions=False).get("/api/sessions")

    assert response.status_code == 200
    return {item["workflow_id"]: item for item in response.json()}


def test_the_session_list_finds_a_running_agent_the_server_does_not_serve() -> None:
    manager = _Manager()
    rows = _sessions(manager)

    assert RUNNING_DIGEST in rows, (
        "a running session disappeared because the server serving the console does not offer "
        f"its agent type; endpoint asked for {manager.asked}"
    )
    assert rows[RUNNING_DIGEST]["is_discovered"] is True


def test_a_manager_that_cannot_answer_the_query_costs_only_discovery() -> None:
    # The manager outlives every server, so a server on this code will meet one still running
    # code without the query. Losing the scan is the cost; losing the sidebar is not.
    rows = _sessions(_Manager(knows_discoverable=False))

    assert set(rows) == {TRACKED}
