# ABOUTME: Archive sweep used to ride every GET /api/sessions poll. It now runs off the read
# path so a cheap existence poll cannot pile updates onto the manager. These pin: GET hides
# NOT_FOUND without writing; the sweep archives only on unambiguous NOT_FOUND; concurrent
# sweeps skip rather than queue; a failed update backs off.

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.web import AgentRegistry, create_agent_harness_app
from temporal_agent_harness.web.app import _archive_vanished_sessions, _run_archive_sweep
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


def test_list_hides_vanished_sessions_without_archiving() -> None:
    with _client([_session(LIVE), _session(GONE)]) as (client, app):
        listed = client.get("/api/sessions")

    assert _ids(listed) == {LIVE}
    assert app.state.manager_handle.updates == []


def test_sweep_archives_a_vanished_session() -> None:
    with _client([_session(LIVE), _session(GONE)]) as (_client_unused, app):
        asyncio.run(_run_archive_sweep(app))

    assert app.state.manager_handle.updates[0].workflow_ids == [GONE]
    assert next(s for s in app.state.manager_handle.sessions if s.workflow_id == GONE).is_archived


def test_an_ambiguous_describe_is_never_archived() -> None:
    # The hazard that turns a tidy-up into data loss: a describe that timed out says nothing
    # about the workflow, and a slow describe is the expected case on this endpoint.
    with _client([_session(LIVE), _session(SLOW)]) as (client, app):
        listed = client.get("/api/sessions")
        asyncio.run(_run_archive_sweep(app))

    assert _ids(listed) == {LIVE, SLOW}
    assert app.state.manager_handle.updates == []


def test_nothing_to_archive_writes_nothing() -> None:
    with _client([_session(LIVE)]) as (_c, app):
        asyncio.run(_run_archive_sweep(app))

    assert app.state.manager_handle.updates == []


def test_the_sweep_does_not_repeat_itself() -> None:
    # Self-limiting: once archived, the session is skipped by the sweep's tracked filter.
    with _client([_session(LIVE), _session(GONE)]) as (_c, app):
        for _ in range(5):
            asyncio.run(_run_archive_sweep(app))

    assert len(app.state.manager_handle.updates) == 1


def test_many_corpses_become_one_update_not_one_each() -> None:
    # 28 is what this dev stack actually had. One update each would be 28 writes on one
    # workflow from one sweep, against a concurrent-update cap of ten.
    corpses = [_session(f"{GONE}-{i}") for i in range(28)]

    async def _state(_temporal, session: Session):
        status = "NOT_FOUND" if session.workflow_id.startswith(GONE) else "RUNNING"
        return {
            **session.__dict__,
            "execution_status": status,
            "closed": status != "RUNNING",
        }

    app = create_agent_harness_app(registry=AgentRegistry())
    app.state.temporal = object()
    app.state.manager_handle = _Manager([_session(LIVE), *corpses])
    with patch("temporal_agent_harness.web.app._session_with_execution_state", _state):
        asyncio.run(_run_archive_sweep(app))

    (update,) = app.state.manager_handle.updates
    assert len(update.workflow_ids) == 28


def test_an_already_archived_session_is_not_archived_again() -> None:
    with _client([_session(LIVE), _session(GONE, archived=True)]) as (_c, app):
        asyncio.run(_run_archive_sweep(app))

    assert app.state.manager_handle.updates == []


def test_concurrent_sweeps_skip_rather_than_queueing_behind_each_other() -> None:
    # N tabs / ticks against one manager is the same shape as the update pileup gates.py
    # exists to prevent, so a sweep already in flight must make the others do nothing at all.
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


def test_an_update_that_cannot_land_backs_off_instead_of_retrying_every_tick() -> None:
    # A manager whose worker predates set_sessions_archived would otherwise be hammered once
    # per sweep forever, with an update that can never succeed.
    with _client([_session(LIVE), _session(GONE)], update_fails=True) as (client, app):
        for _ in range(5):
            asyncio.run(_run_archive_sweep(app))
        listed = client.get("/api/sessions")

    assert len(app.state.manager_handle.updates) == 1
    # List still serves: the corpse is hidden from the default view even before archive lands.
    assert _ids(listed) == {LIVE}


def test_the_backoff_expires_so_a_recovered_manager_is_swept() -> None:
    with _client([_session(LIVE), _session(GONE)], update_fails=True) as (_c, app):
        asyncio.run(_run_archive_sweep(app))
        app.state.manager_handle.update_fails = False
        app.state.archive_retry_after = 0.0
        asyncio.run(_run_archive_sweep(app))

    assert len(app.state.manager_handle.updates) == 2


def test_looking_at_the_archived_state_does_not_change_it() -> None:
    with _client([_session(LIVE), _session(GONE)]) as (client, app):
        listed = client.get("/api/sessions?include_archived=true")

    assert _ids(listed) == {LIVE, GONE}
    assert app.state.manager_handle.updates == []


def test_existence_view_is_manager_only() -> None:
    with _client([_session(LIVE), _session(GONE)]) as (client, app):
        body = client.get("/api/sessions?view=ids").json()

    assert body["revision"]
    assert {row["workflow_id"] for row in body["sessions"]} == {LIVE, GONE}
    assert app.state.manager_handle.updates == []


def test_get_session_by_id_includes_archived() -> None:
    with _client([_session(LIVE), _session(GONE, archived=True)]) as (client, _app):
        body = client.get(f"/api/sessions/{GONE}").json()

    assert body["workflow_id"] == GONE
    assert body["is_archived"] is True


def test_get_session_unknown_id_is_404() -> None:
    with _client([_session(LIVE)]) as (client, _app):
        response = client.get("/api/sessions/agent-session-missing")

    assert response.status_code == 404
