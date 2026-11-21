"""FastAPI app factory for the harness web UI/API."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.converter import ExternalStorage
from temporalio.envconfig import ClientConfig
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_kit.agent_client import (
    JoinedTurnError,
    MidTurnRejectedError,
    AgentClient,
    AgentStreamOutput,
    AgentTurnError,
    AgentTurnTimeout,
    CallbackResultError,
    ToolApprovalError,
)
from temporal_agent_kit.agent_protocol import AgentEvent, AgentStatus
from temporal_agent_kit.plugin import AgentKitPlugin
from temporal_agent_kit.ui import packaged_ui_dist
from temporal_agent_kit.utils.large_payload import DEFAULT_PAYLOAD_STORAGE
from temporal_agent_kit.web.agent_starts import (
    InvalidStartData,
    validated_start_data,
)
from temporal_agent_kit.web.file_view import (
    FileViewError,
    FileViewRequest,
    OKFGraphRequest,
    okf_graph,
    view_file,
)
from temporal_agent_kit.web.nexus_registry_store import RegistryStoreError
from temporal_agent_kit.web.registry import (
    AgentDescriptor,
    AgentRegistration,
    AgentRegistry,
    InvalidRegistration,
    registered_agent,
    registration_of,
    registry_view,
)
from temporal_agent_kit.web.registry_changes import (
    RegistryBusy,
    RegistryConflict,
    UnknownAgent,
    change_registry,
    deleted,
    registered,
    retired,
)
from temporal_agent_kit.web.registry_store import (
    AgentRegistryStore,
    RegistrySnapshot,
    RegistryStoreFactory,
)
from temporal_agent_kit.web.registry_workflow import workflow_registry_store
from temporal_agent_kit.web.sessions import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    InvalidPageToken,
    Session,
    SessionIdTaken,
    SessionPage,
    get_session,
    list_sessions,
    start_session,
)
from temporal_agent_kit.web.task_queue_status import describe_task_queue_workers
from temporal_agent_kit.web.temporal_ui import temporal_workflows_url

class RegisterAgentsRequestBody(BaseModel):
    """Agents to register together: all of them or none."""

    model_config = ConfigDict(extra="forbid")

    agents: list[AgentRegistration] = Field(min_length=1)


class CreateSessionRequestBody(BaseModel):
    agent_workflow_type: str
    # Optional caller-chosen workflow id, instead of a minted ``agent-session-{uuid4}``. The UI
    # does not set it — a minted id is right when the session has no identity outside the
    # harness. An integration whose conversation identity comes from elsewhere (a Slack thread)
    # sets it so the session id is derivable from that identity instead of needing a mapping
    # table. Creation is idempotent while that session runs.
    session_id: str | None = None
    # The agent's init data, as JSON — see each agent's ``init_data`` in ``GET /api/agents``.
    data: dict[str, Any] | None = None


class ChatRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    message: str | dict[str, Any]
    # Optional idempotency key, forwarded as the update id. For a caller whose delivery can be
    # repeated — a chat-platform webhook is redelivered routinely — set this to the platform's
    # own event id and a redelivery re-issues the SAME update, getting the original acceptance
    # back rather than dispatching the message twice. The UI leaves it unset: a click is one
    # delivery, so every send should be its own dispatch.
    request_id: str | None = None


class ToolApprovalRequestBody(BaseModel):
    session_id: str
    tool_id: str
    approved: bool
    reason: str | None = None
    remember: bool = False


class CallbackResultRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    tool_id: str
    # Exactly one of result / error is meaningful: ``result`` is the JSON-native value the client
    # produced (validated server-side against the callback tool's declared output type); ``error``
    # reports that the client could not fulfill the call.
    result: Any = None
    error: str | None = None


def create_agent_kit_app(
    *,
    registry_store: RegistryStoreFactory = workflow_registry_store,
    registrations: Sequence[AgentRegistration] = (),
    static_dir: Path | str | None = None,
    index_file: str = "index.html",
    states_file: str | None = None,
    large_payload_offload: ExternalStorage | None = DEFAULT_PAYLOAD_STORAGE,
) -> FastAPI:
    """Create the reusable harness web API.

    Args:
        registry_store: Builds the store the server keeps its agent registry in, from its
            Temporal client. The default keeps it in a Temporal workflow, served by a worker
            inside this app.
        registrations: Agents to register when the app starts, all of them or none, as
            ``POST /api/agents`` registers them. A refused registration fails the startup.
        static_dir: Optional directory containing static UI assets. When omitted,
            the packaged Vite UI is served if it is present in the installed package.
        index_file: File in ``static_dir`` served from ``/``.
        states_file: Optional file in ``static_dir`` served from ``/states``.
        large_payload_offload: Where oversized payloads are offloaded, forwarded to
            ``AgentKitPlugin``. Must match what every agent worker uses, or this server can't
            read their payloads. The default is single-host only; build
            :func:`~temporal_agent_kit.utils.large_payload.s3_payload_storage` in your
            own async startup and pass it for a multi-host deploy.
    """

    static_path = Path(static_dir) if static_dir is not None else packaged_ui_dist()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        connect_config = ClientConfig.load_client_connect_config()
        # The harness plugin is where the data converter is DEFINED — the same one every agent
        # worker gets by adding the same plugin. An offloaded payload is only readable by a
        # process using the matching converter, so this app must not spell one out of its own.
        # The plugin's activity registration is a WORKER concern and this app hosts no worker,
        # so it simply doesn't apply here.
        app.state.temporal = await Client.connect(
            **connect_config,
            plugins=[AgentKitPlugin(large_payload_offload=large_payload_offload)],
        )
        app.state.temporal_workflows_url = temporal_workflows_url(
            connect_config.get("target_host"), connect_config.get("namespace")
        )

        async with registry_store(app.state.temporal) as store:
            app.state.registry_store = store
            if startup_agents:
                await change_registry(store, lambda snapshot: registered(snapshot, startup_agents))
            yield

    # Checked before the app is built, so a bad registration is reported before anything starts.
    startup_agents = [registered_agent(registration) for registration in registrations]
    registered(RegistrySnapshot(), startup_agents)

    async def current_registry() -> AgentRegistry:
        store: AgentRegistryStore = app.state.registry_store
        return registry_view((await store.read()).snapshot)

    app = FastAPI(lifespan=lifespan)
    # Filled at startup; None means the Temporal Web UI's address is unknown.
    app.state.temporal_workflows_url = None
    # Open CORS: this is a local dev server whose API is meant to be driven by any client,
    # including a standalone HTML page opened from disk (origin ``null``).
    # Nothing here is credentialed, so the wildcard is safe.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if static_path is not None:
        _mount_static_ui(
            app,
            static_path=static_path,
            index_file=index_file,
            states_file=states_file,
        )
    else:

        @app.get("/")
        async def index():
            return JSONResponse({"status": "ok", "service": "temporal-agent-kit"})

    @app.get("/api/agents")
    async def list_agents():
        """The launchable agents, each with whether a worker is actually polling its queue.

        The readiness half is live rather than registry-derived, so this is ``no-store`` and
        the UI refetches on every agent-picker open — a worker can come up or go down between
        one look and the next, and a cached "Ready" is the exact lie this endpoint exists to
        stop telling.
        """
        agents = (await current_registry()).agents
        workers = await describe_task_queue_workers(
            app.state.temporal,
            (agent.task_queue for agent in agents),
        )
        content = {
            "agents": [
                {
                    **_agent_content(agent),
                    "worker": asdict(workers.for_task_queue(agent.task_queue)),
                }
                for agent in agents
            ]
        }
        return JSONResponse(content=content, headers={"Cache-Control": "no-store"})

    @app.get("/api/agents/{key}/registration")
    async def agent_registration(key: str):
        """``key`` as it was registered: its task queue without TASK_QUEUE_PREFIX and its schema
        document, so sending it to ``POST /api/agents`` registers it again."""
        store: AgentRegistryStore = app.state.registry_store
        agent = (await store.read()).snapshot.agents.get(key)
        if agent is None:
            raise UnknownAgent(key)
        return JSONResponse(
            content=registration_of(agent).model_dump(mode="json", by_alias=True),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/agents")
    async def register_agents(req: RegisterAgentsRequestBody):
        """Register every agent in the body, or none of them. A registered key is replaced and
        made active again."""
        agents = [registered_agent(registration) for registration in req.agents]
        snapshot = await change_registry(
            app.state.registry_store, lambda current: registered(current, agents)
        )
        keys = {agent.key for agent in agents}
        view = registry_view(snapshot)
        return JSONResponse(
            content={"agents": [_agent_content(a) for a in view.agents if a.key in keys]},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/agents/{key}/retire")
    async def retire_agent(key: str):
        """Stop offering ``key`` for new sessions. Its sessions stay listed and usable."""
        await change_registry(app.state.registry_store, lambda current: retired(current, key))
        return JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})

    @app.delete("/api/agents/{key}")
    async def delete_agent(key: str):
        """Remove ``key`` from the registry. Its sessions leave the session list."""
        await change_registry(app.state.registry_store, lambda current: deleted(current, key))
        return JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions")
    async def list_sessions_route(
        page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        page_token: str | None = None,
    ):
        """One page of sessions, newest first. Pass ``next_page_token`` back as ``page_token``
        for the next; it is ``null`` on the last page."""
        page = await list_sessions(
            app.state.temporal,
            await current_registry(),
            page_size=page_size,
            page_token=page_token,
        )
        return JSONResponse(
            content=_session_page_content(page, app.state.temporal_workflows_url),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/sessions/{session_id}")
    async def get_session_route(session_id: str):
        """One session by id, for a session that is not on a page the caller has loaded."""
        session = await get_session(app.state.temporal, await current_registry(), session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"No session {session_id!r}.")
        return JSONResponse(
            content=_session_content(session, app.state.temporal_workflows_url),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequestBody):
        registry = await current_registry()
        agent = registry.by_workflow_type(req.agent_workflow_type)
        if agent is None:
            known = [descriptor.workflow_type for descriptor in registry.agents]
            raise HTTPException(
                status_code=422,
                detail=f"Unknown agent type {req.agent_workflow_type!r}. Known agents: {known}",
            )
        if agent.retired:
            raise HTTPException(
                status_code=409,
                detail=f"{agent.key!r} is retired: its sessions can be used, but no new ones "
                "start. Register it again to start new sessions.",
            )
        data = validated_start_data(req.agent_workflow_type, agent.start, req.data)
        session = await start_session(
            app.state.temporal,
            agent,
            registry,
            data=data,
            session_id=req.session_id,
        )
        return _session_content(session, app.state.temporal_workflows_url)

    @app.get("/api/workflow-status/{workflow_id}")
    async def workflow_status(workflow_id: str):
        content = await _workflow_execution_state(app.state.temporal, workflow_id)
        return JSONResponse(content=content, headers={"Cache-Control": "no-store"})

    @app.get("/api/status/{session_id}")
    async def get_status(session_id: str):
        client = AgentClient(temporal=app.state.temporal, workflow_id=session_id)
        status = await client.get_status()
        content = TypeAdapter(AgentStatus).dump_python(status, mode="json")
        return JSONResponse(content=content, headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{session_id}/close")
    async def close_session(session_id: str):
        """Gracefully stop the agent workflow via the harness ``close`` signal: it winds down its
        turn loop and auto-denies any pending approvals/callbacks. Lets a client implement abort
        (stop the durable agent), rather than only dropping its own stream."""
        handle = app.state.temporal.get_workflow_handle(session_id)
        await handle.signal("close")
        return JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/api/agent-interface/{session_id}")
    async def agent_interface(session_id: str):
        client = AgentClient(temporal=app.state.temporal, workflow_id=session_id)
        functions = await client.get_agent_interface()
        return JSONResponse(content=[fn.model_dump(mode="json") for fn in functions])

    @app.get("/api/attach")
    async def attach(session_id: str, from_offset: int = 0) -> StreamingResponse:
        client = AgentClient(temporal=app.state.temporal, workflow_id=session_id)
        return StreamingResponse(
            await client.attach(on_item=_yield_item, from_offset=from_offset),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @app.post("/api/approve")
    async def approve_tool(req: ToolApprovalRequestBody):
        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        result = await client.approve_tool(
            req.tool_id,
            approved=req.approved,
            reason=req.reason,
            remember=req.remember,
        )
        return JSONResponse(content=asdict(result), headers={"Cache-Control": "no-store"})

    @app.post("/api/callback-result")
    async def provide_callback_result(req: CallbackResultRequestBody):
        """Fulfill a pending callback tool call: a client that executed the tool on its own
        machine submits the result (or an error), keyed by the ``tool_id`` from the
        ``callback_requested`` event. Forwards to the workflow's ``provide_callback_result``
        update; the result is validated against the tool's declared output type there."""
        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        result = await client.provide_callback_result(
            req.tool_id, result=req.result, error=req.error
        )
        return JSONResponse(content=asdict(result), headers={"Cache-Control": "no-store"})

    @app.post("/api/files/view")
    async def view_mounted_file(req: FileViewRequest):
        """One page of a file in an activity-backed Code Mode mount, read from its store as it
        is now (not as it was at any point in the agent's history). The request carries the
        mount's ``FileIndex.source``; nothing about the agent's workflow is touched."""
        chunk = await view_file(app.state.temporal, req)
        return JSONResponse(
            content=chunk.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/files/okf-graph")
    async def okf_bundle_graph(req: OKFGraphRequest):
        """An OKF bundle mount's concepts and the links between them, walked from its store as
        it is now. Like ``/api/files/view``, nothing about the agent's workflow is touched."""
        graph = await okf_graph(app.state.temporal, req)
        return JSONResponse(
            content=graph.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/messages")
    async def submit_message(req: ChatRequestBody):
        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        if isinstance(req.message, str):
            msg_type, payload = "ask", {"text": req.message}
        else:
            msg_type, payload = req.message["type"], req.message.get("payload") or {}

        result = await client.submit_message(msg_type, payload, update_id=_update_id(req))
        return JSONResponse(content=asdict(result), headers={"Cache-Control": "no-store"})

    @app.post("/api/chat")
    async def chat(req: ChatRequestBody):
        def on_item(item: AgentStreamOutput, resume_offset: int) -> bytes:
            match item:
                case AgentTurnTimeout():
                    return _sse(
                        STREAM_ERROR_SSE_EVENT,
                        {"kind": "timeout", "message": str(item)},
                        resume_offset,
                    )
                case AgentTurnError():
                    return _sse(
                        STREAM_ERROR_SSE_EVENT,
                        {"kind": "agent", "message": str(item)},
                        resume_offset,
                    )
                case _:
                    return _yield_item(item, resume_offset)

        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        if isinstance(req.message, str):
            msg_type, payload = "ask", {"text": req.message}
        else:
            msg_type, payload = req.message["type"], req.message.get("payload") or {}

        return StreamingResponse(
            await client.send_message(
                msg_type, payload, on_item=on_item, update_id=_update_id(req)
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @app.exception_handler(JoinedTurnError)
    async def joined_turn_handler(request, exc):
        # 409 like mid_turn_rejected, but it is NOT a rejection: the message was accepted and is
        # running inside the turn it joined. Only the per-turn STREAM is unavailable, so the
        # body carries the accepted reply — a caller that wants to watch the work attaches and
        # follows ``message_id``.
        return JSONResponse(
            status_code=409,
            content={
                "error": "joined_turn",
                "message": str(exc),
                "reply": asdict(exc.reply),
            },
        )

    @app.exception_handler(RegistryConflict)
    async def registry_conflict_handler(request, exc):
        return JSONResponse(
            status_code=409, content={"error": "registry_conflict", "message": str(exc)}
        )

    @app.exception_handler(UnknownAgent)
    async def unknown_agent_handler(request, exc):
        return JSONResponse(
            status_code=404, content={"error": "unknown_agent", "message": str(exc)}
        )

    @app.exception_handler(InvalidRegistration)
    async def invalid_registration_handler(request, exc):
        return JSONResponse(
            status_code=422, content={"error": "invalid_registration", "message": str(exc)}
        )

    @app.exception_handler(RegistryStoreError)
    async def registry_store_error_handler(request, exc):
        # 502: the registry lives behind a Nexus endpoint, whose operation failed.
        return JSONResponse(
            status_code=502, content={"error": "registry_store_error", "message": str(exc)}
        )

    @app.exception_handler(RegistryBusy)
    async def registry_busy_handler(request, exc):
        return JSONResponse(
            status_code=503, content={"error": "registry_busy", "message": str(exc)}
        )

    @app.exception_handler(InvalidPageToken)
    async def invalid_page_token_handler(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SessionIdTaken)
    async def session_id_taken_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidStartData)
    async def invalid_start_data_handler(request, exc):
        # 422: the start was not attempted.
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_init_data", "message": str(exc)},
        )

    @app.exception_handler(MidTurnRejectedError)
    async def mid_turn_rejected_handler(request, exc):
        # 409, not 429: the handler declared it must not run mid-turn, so this is a
        # conflict with current state rather than a rate limit — resending will not help
        # until the agent goes idle.
        return JSONResponse(
            status_code=409,
            content={"error": "mid_turn_rejected", "message": str(exc)},
        )

    @app.exception_handler(ToolApprovalError)
    async def tool_approval_handler(request, exc):
        return JSONResponse(
            status_code=409,
            content={
                "error": exc.error_type or "tool_approval_error",
                "message": str(exc),
            },
        )

    @app.exception_handler(FileViewError)
    async def file_view_handler(request, exc: FileViewError):
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.error, "message": str(exc)},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(CallbackResultError)
    async def callback_result_handler(request, exc):
        return JSONResponse(
            status_code=409,
            content={
                "error": exc.error_type or "callback_result_error",
                "message": str(exc),
            },
        )

    return app


def _agent_content(agent: AgentDescriptor) -> dict[str, object]:
    content: dict[str, object] = asdict(agent)
    # {"required": bool, "schema": {...}}, or null when the agent takes no init data (or was
    # registered without a schema document).
    content["init_data"] = agent.start.init_data if agent.start is not None else None
    return content


async def _workflow_execution_state(
    temporal: Client,
    workflow_id: str,
) -> dict[str, object]:
    handle = temporal.get_workflow_handle(workflow_id)
    try:
        desc = await handle.describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
        return {
            "workflow_id": workflow_id,
            "execution_status": "NOT_FOUND",
            "closed": True,
        }

    return {
        "workflow_id": workflow_id,
        "execution_status": desc.status.name,
        "closed": desc.status != WorkflowExecutionStatus.RUNNING,
    }


def _session_content(session: Session, temporal_workflows_url: str | None) -> dict[str, object]:
    content: dict[str, object] = asdict(session)
    if temporal_workflows_url is not None:
        content["temporal_workflows_url"] = temporal_workflows_url
    return content


def _session_page_content(page: SessionPage, temporal_workflows_url: str | None) -> dict[str, object]:
    return {
        "sessions": [_session_content(session, temporal_workflows_url) for session in page.sessions],
        "next_page_token": page.next_page_token,
    }


def _mount_static_ui(
    app: FastAPI,
    *,
    static_path: Path,
    index_file: str,
    states_file: str | None,
) -> None:
    if not static_path.exists():
        raise ValueError(f"Static UI directory does not exist: {static_path}")

    # follow_symlink: a dist installed as symlinks (e.g. Bazel runfiles) resolves outside its
    # directory, which StaticFiles otherwise answers with 404.
    assets_path = static_path / "assets"
    if assets_path.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_path, follow_symlink=True), name="assets")
    app.mount("/static", StaticFiles(directory=static_path, follow_symlink=True), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static_path / index_file)

    if states_file is not None:

        @app.get("/states")
        async def states():
            return FileResponse(static_path / states_file)

    @app.get("/{asset_name}")
    async def top_level_asset(asset_name: str):
        asset_path = static_path / asset_name
        if asset_path.is_file():
            return FileResponse(asset_path)
        raise HTTPException(status_code=404)


# SSE event name for a failure the CLIENT side of the stream produced — a turn timeout, or
# this turn's own ``message_handler_error`` surfaced as the caller's failure signal. Deliberately
# NOT an ``AgentEventType``: nothing published it on the agent's stream, and the frame carries no
# turn/message metadata, so a consumer must be able to tell it apart from a real agent event.
STREAM_ERROR_SSE_EVENT = "stream_error"


def _update_id(req: ChatRequestBody) -> str | None:
    """The update idempotency key for a message send, or ``None`` to let Temporal mint one.

    Namespaced so a caller's ``request_id`` can never collide with an update id the harness
    mints for its own purposes (``send-``/``approve-`` in the Nexus adapter, for instance).
    """
    return f"msg-{req.request_id}" if req.request_id else None


def _sse(event: str, data: dict, resume_offset: int | None = None) -> bytes:
    payload = {**data}
    if resume_offset is not None:
        payload["resume_offset"] = resume_offset
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def _yield_item(item, resume_offset: int | None = None) -> bytes:
    if isinstance(item, AgentEvent):
        payload = item.event
        data = {
            **payload.model_dump(mode="json"),
            "agent_id": item.agent_id,
            "turn_id": item.turn_id,
            "turn_number": item.turn_number,
            # ``None`` for the events that belong to no single message (turn brackets) — sent
            # explicitly rather than omitted, so a consumer can tell "unattributed" from "an
            # older server that didn't say".
            "message_id": item.message_id,
            "timestamp": item.timestamp,
        }
        return _sse(payload.type, data, resume_offset)
    return b""


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
