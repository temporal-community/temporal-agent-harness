# ABOUTME: Nothing ever removed an entry from the manager's session list while the namespace's
# retention kept deleting the workflows under it, so the list grew monotonically and every corpse
# cost a describe and a history scan on every ten-second poll. Archiving sheds them without losing
# them; and a discovered session needs a label that is not shared with every sibling of its agent.

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app
from temporal_agent_harness.web.app import _discovered_label
from temporal_agent_harness.web.session_manager import (
    SetSessionsArchivedRequest,
    Session,
    SessionManagerWorkflow,
)

LIVE = "agent-session-live"
DEAD = "agent-session-dead"


def _session(workflow_id: str, *, archived: bool = False) -> Session:
    return Session(
        workflow_id=workflow_id,
        created_at=0.0,
        label=workflow_id,
        agent_workflow_type="HelloAgent",
        is_archived=archived,
    )


class _Manager:
    """Enough of the manager handle to record what the endpoint asks of the workflow."""

    def __init__(self, sessions: list[Session]) -> None:
        self.sessions = sessions
        self.updates: list[SetSessionsArchivedRequest] = []

    async def query(self, name, result_type=None):
        if result_type is AgentRegistry:
            return AgentRegistry()
        return list(self.sessions)

    async def execute_update(self, name, request, result_type=None):
        self.updates.append(request)
        return SessionManagerWorkflow.set_sessions_archived(self, request)  # type: ignore[arg-type]

    # set_sessions_archived reads this attribute off ``self``; reusing the real handler here
    # keeps the test honest about what the workflow actually does with the request.
    @property
    def _sessions(self) -> list[Session]:
        return self.sessions


class _Handle:
    def __init__(self, status: WorkflowExecutionStatus | None) -> None:
        self.status = status
        self.signals: list[str] = []

    async def describe(self):
        if self.status is None:
            raise RPCError("gone", RPCStatusCode.NOT_FOUND, b"")
        return self

    async def signal(self, name: str) -> None:
        self.signals.append(name)


class _Temporal:
    def __init__(self) -> None:
        self.handles = {
            LIVE: _Handle(WorkflowExecutionStatus.RUNNING),
            DEAD: _Handle(WorkflowExecutionStatus.COMPLETED),
        }

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        # A real client hands back a handle for any id; whether it exists is the describe's
        # answer, which is exactly the case this test's unknown id exercises.
        return self.handles.setdefault(workflow_id, _Handle(None))


@contextmanager
def _client(sessions: list[Session]):
    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.temporal = _Temporal()
    app.state.manager_handle = _Manager(sessions)

    async def _state(_temporal, session: Session):
        return {**session.__dict__, "execution_status": "RUNNING", "closed": False}

    async def _discover(_temporal, _registry, _known):
        return []

    with (
        patch("temporal_agent_harness.web.app._session_with_execution_state", _state),
        patch("temporal_agent_harness.web.app._discover_untracked_sessions", _discover),
    ):
        yield TestClient(app), app.state.manager_handle, app.state.temporal


def test_archiving_hides_a_session_from_the_list() -> None:
    with _client([_session(LIVE), _session(DEAD)]) as (client, manager, _temporal):
        client.post("/api/sessions/archive", json={"workflow_ids": [DEAD]})
        listed = client.get("/api/sessions").json()

    assert [item["workflow_id"] for item in listed] == [LIVE]
    assert manager.sessions[1].is_archived is True


def test_an_archived_session_is_still_there_to_be_asked_for() -> None:
    # A flag, not a removal: a deep link into an archived session has to keep resolving.
    with _client([_session(LIVE), _session(DEAD, archived=True)]) as (client, _m, _t):
        listed = client.get("/api/sessions?include_archived=true").json()

    assert {item["workflow_id"] for item in listed} == {LIVE, DEAD}


def test_archiving_a_running_session_closes_it_first() -> None:
    # Otherwise hiding a session would leave a live agent running where nobody will look.
    with _client([_session(LIVE)]) as (client, _m, temporal):
        body = client.post("/api/sessions/archive", json={"workflow_ids": [LIVE]}).json()

    assert body["closed"] == [LIVE]
    assert temporal.handles[LIVE].signals == ["close"]


def test_archiving_an_already_finished_session_signals_nothing() -> None:
    with _client([_session(DEAD)]) as (client, _m, temporal):
        body = client.post("/api/sessions/archive", json={"workflow_ids": [DEAD]}).json()

    assert body["closed"] == []
    assert temporal.handles[DEAD].signals == []


def test_restoring_only_unhides() -> None:
    with _client([_session(DEAD, archived=True)]) as (client, _m, temporal):
        body = client.post(
            "/api/sessions/archive", json={"workflow_ids": [DEAD], "is_archived": False}
        ).json()

    assert body["archived"] == [DEAD]
    assert temporal.handles[DEAD].signals == []


def test_unknown_ids_do_not_fail_the_batch() -> None:
    # One tab can archive what another still lists, and the bulk case is when that matters most.
    with _client([_session(DEAD)]) as (client, _m, _t):
        body = client.post(
            "/api/sessions/archive", json={"workflow_ids": [DEAD, "agent-session-vanished"]}
        ).json()

    assert body["archived"] == [DEAD]


def test_discovered_labels_are_distinct_per_workflow() -> None:
    # The bug: three scheduled runs of one agent all arrived as "Scheduled Digest".
    ids = [
        "scheduled-daily-digest-54ba46cb-0829-4ff7-951b-b6e95a0acf83",
        "scheduled-daily-digest-7738a705-716a-495c-a77d-3d383281494b",
        "scheduled-daily-digest-e44c6683-a2b1-4e06-836e-0dd9b2a441e6",
    ]
    labels = [_discovered_label("Scheduled Digest", workflow_id) for workflow_id in ids]

    assert len(set(labels)) == len(ids)
    assert all(label.startswith("Scheduled Digest ") for label in labels)
    # Stable, so a row's name does not change from one poll to the next.
    assert labels[0] == _discovered_label("Scheduled Digest", ids[0])


def test_a_discovered_label_reads_as_an_identifier_not_a_word() -> None:
    # Slicing the id gave "Test Agent utside", which reads as a typo rather than an id.
    suffix = _discovered_label("Test Agent", "agent-session-outside").removeprefix("Test Agent ")

    assert len(suffix) == 6
    assert all(character in "0123456789abcdef" for character in suffix)
