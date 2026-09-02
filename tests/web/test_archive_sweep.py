# ABOUTME: The archive sweep is a WRITE on a read path that every open tab hits every ten seconds,
# against a single workflow Temporal caps at ten concurrent updates. These pin the three things that
# make that defensible: it archives only on an unambiguous NOT_FOUND, it never writes twice for the
# same session, and a room full of tabs cannot turn one sweep into a pileup.

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app
from temporal_agent_harness.web.app import _archive_vanished_sessions
from temporal_agent_harness.web.session_manager import Session, SetSessionsArchivedRequest

GONE = "agent-session-gone"
SLOW = "agent-session-slow"
LIVE = "agent-session-live"


def _session(workflow_id: str, *, archived: bool = False, discovered: bool = False) -> Session:
    return Session(
        workflow_id=workflow_id,
        created_at=0.0,
        label=workflow_id,
        agent_workflow_type="HelloAgent",
        is_archived=archived,
        is_discovered=discovered,
    )


class _Manager:
    def __init__(self, sessions: list[Session], *, update_fails: bool = False) -> None:
        self.sessions = sessions
        self.update_fails = update_fails
        self.updates: list[SetSessionsArchivedRequest] = []
        self.update_gate: asyncio.Event | None = None

    async def query(self, name, result_type=None):
        if result_type is AgentRegistry:
            return AgentRegistry()
        return list(self.sessions)

    async def execute_update(self, name, request, result_type=None):
        self.updates.append(request)
        if self.update_gate is not None:
            await self.update_gate.wait()
        if self.update_fails:
            raise RPCError("no such update handler", RPCStatusCode.NOT_FOUND, b"")
        wanted = set(request.workflow_ids)
        changed = [
            session
            for session in self.sessions
            if session.workflow_id in wanted and not session.is_archived
        ]
        for session in changed:
            session.is_archived = request.is_archived
        return changed


@contextmanager
def _client(sessions: list[Session], *, update_fails: bool = False):
    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.temporal = object()
    app.state.manager_handle = _Manager(sessions, update_fails=update_fails)

    async def _state(_temporal, session: Session):
        status = {GONE: "NOT_FOUND", SLOW: "UNKNOWN", LIVE: "RUNNING"}[session.workflow_id]
        return {
            **session.__dict__,
            "execution_status": status,
            "closed": status != "RUNNING",
        }

    async def _discover(_temporal, _registry, _known):
        return []

    with (
        patch("temporal_agent_harness.web.app._session_with_execution_state", _state),
        patch("temporal_agent_harness.web.app._discover_untracked_sessions", _discover),
    ):
        yield TestClient(app), app


def _ids(response) -> set[str]:
    return {item["workflow_id"] for item in response.json()}


def test_a_vanished_session_is_archived_and_gone_from_the_same_response() -> None:
    with _client([_session(LIVE), _session(GONE)]) as (client, app):
        listed = client.get("/api/sessions")

    assert _ids(listed) == {LIVE}
    assert app.state.manager_handle.updates[0].workflow_ids == [GONE]


def test_an_ambiguous_describe_is_never_archived() -> None:
    # The hazard that turns a tidy-up into data loss: a describe that timed out says nothing
    # about the workflow, and a slow describe is the expected case on this endpoint.
    with _client([_session(LIVE), _session(SLOW)]) as (client, app):
        listed = client.get("/api/sessions")

    assert _ids(listed) == {LIVE, SLOW}
    assert app.state.manager_handle.updates == []


def test_nothing_to_archive_writes_nothing() -> None:
    with _client([_session(LIVE)]) as (client, app):
        client.get("/api/sessions")

    assert app.state.manager_handle.updates == []


def test_the_sweep_does_not_repeat_itself() -> None:
    # Self-limiting by construction: once archived, the session is filtered out before the
    # enrichment that would describe it, so it can never be seen NOT_FOUND again.
    with _client([_session(LIVE), _session(GONE)]) as (client, app):
        for _ in range(5):
            client.get("/api/sessions")

    assert len(app.state.manager_handle.updates) == 1


def test_many_corpses_become_one_update_not_one_each() -> None:
    # 28 is what this dev stack actually had. One update each would be 28 writes on one
    # workflow from one poll, against a concurrent-update cap of ten.
    corpses = [_session(GONE) for _ in range(28)]
    with _client([_session(LIVE), *corpses]) as (client, app):
        client.get("/api/sessions")

    (update,) = app.state.manager_handle.updates
    assert len(update.workflow_ids) == 28


def test_an_already_archived_session_is_not_archived_again() -> None:
    with _client([_session(LIVE), _session(GONE, archived=True)]) as (client, app):
        client.get("/api/sessions")

    assert app.state.manager_handle.updates == []


def test_concurrent_sweeps_skip_rather_than_queueing_behind_each_other() -> None:
    # N tabs polling one manager is the same shape as the update pileup gates.py exists to
    # prevent, so a sweep already in flight must make the others do nothing at all.
    #
    # Driven against the sweep directly rather than through TestClient, which does not give
    # concurrent requests one event loop to contend on — and an asyncio.Lock only means
    # anything within one.
    async def drive() -> int:
        app = create_agent_harness_app(registry=AgentRegistry())
        app.state.archive_sweep = asyncio.Lock()
        app.state.archive_retry_after = 0.0
        manager = _Manager([_session(GONE)])
        manager.update_gate = asyncio.Event()
        app.state.manager_handle = manager

        sweeps = [
            asyncio.create_task(_archive_vanished_sessions(app, [GONE])) for _ in range(10)
        ]
        while not manager.updates:
            await asyncio.sleep(0)
        manager.update_gate.set()
        await asyncio.gather(*sweeps)
        return len(manager.updates)

    assert asyncio.run(drive()) == 1


def test_an_update_that_cannot_land_backs_off_instead_of_retrying_every_poll() -> None:
    # A manager whose worker predates set_sessions_archived would otherwise be hammered once
    # per poll per tab, forever, with an update that can never succeed.
    with _client([_session(LIVE), _session(GONE)], update_fails=True) as (client, app):
        for _ in range(5):
            listed = client.get("/api/sessions")

    assert len(app.state.manager_handle.updates) == 1
    # And the failure costs nothing else: the list is still served, corpse included.
    assert _ids(listed) == {LIVE, GONE}


def test_the_backoff_expires_so_a_recovered_manager_is_swept() -> None:
    with _client([_session(LIVE), _session(GONE)], update_fails=True) as (client, app):
        client.get("/api/sessions")
        app.state.manager_handle.update_fails = False
        app.state.archive_retry_after = 0.0
        client.get("/api/sessions")

    assert len(app.state.manager_handle.updates) == 2


def test_looking_at_the_archived_state_does_not_change_it() -> None:
    with _client([_session(LIVE), _session(GONE)]) as (client, app):
        listed = client.get("/api/sessions?include_archived=true")

    assert _ids(listed) == {LIVE, GONE}
    assert app.state.manager_handle.updates == []
