"""FastAPI app factory for the harness web UI/API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, TypeAdapter
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowHandle,
    WorkflowQueryFailedError,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.envconfig import ClientConfig
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_client import (
    AgentBusyError,
    AgentClient,
    AgentStreamOutput,
    AgentTurnError,
    AgentTurnTimeout,
    CallbackResultError,
    StaleTurnError,
    ToolApprovalError,
)
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentStatus,
    OperatorCommand,
    OperatorCommandResult,
    SEND_AGENT_MESSAGE_UPDATE,
)
from temporal_agent_harness.harness.stream_merge import StreamPosition
from temporal_agent_harness.ui import packaged_ui_dist
from temporal_agent_harness.utils.large_payload import with_large_payload_offload
from temporal_agent_harness.web.registry import load_agent_registry
from temporal_agent_harness.web.session_manager import (
    SESSION_MANAGER_ID,
    SESSION_MANAGER_TASK_QUEUE,
    AgentRegistry,
    CreateSessionRequest as ManagerCreateSessionRequest,
    Session,
    SessionManagerWorkflow,
)

RegistrySource = AgentRegistry | Callable[[], AgentRegistry]
_SESSION_PREVIEW_HISTORY_PAGE_SIZE = 16
_SESSION_PREVIEW_HISTORY_MAX_EVENTS = 96
_SESSION_PREVIEW_HISTORY_RPC_TIMEOUT = timedelta(seconds=1)


class CreateSessionRequestBody(BaseModel):
    agent_workflow_type: str
    is_message_queuing_enabled: bool = False


class ChatRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    message: str | dict[str, Any]
    expected_turn: int


class ToolApprovalRequestBody(BaseModel):
    session_id: str
    tool_id: str
    approved: bool
    reason: str | None = None
    remember: bool = False


class OperatorCommandRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    name: str
    arg: str | None = None


class CallbackResultRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    tool_id: str
    # Exactly one of result / error is meaningful: ``result`` is the JSON-native value the client
    # produced (validated server-side against the callback tool's declared output type); ``error``
    # reports that the client could not fulfill the call.
    result: Any = None
    error: str | None = None


def create_agent_harness_app(
    *,
    registry: RegistrySource | None = None,
    registry_path: Path | str | None = None,
    manager_workflow_id: str = SESSION_MANAGER_ID,
    manager_task_queue: str = SESSION_MANAGER_TASK_QUEUE,
    static_dir: Path | str | None = None,
    index_file: str = "index.html",
    states_file: str | None = None,
) -> FastAPI:
    """Create the reusable harness web API.

    Args:
        registry: In-memory registry or a callable that returns one at startup.
        registry_path: TOML registry path. Mutually exclusive with ``registry``.
        manager_workflow_id: Deterministic workflow ID for the session manager.
        manager_task_queue: Task queue where the session manager worker polls.
        static_dir: Optional directory containing static UI assets. When omitted,
            the packaged Vite UI is served if it is present in the installed package.
        index_file: File in ``static_dir`` served from ``/``.
        states_file: Optional file in ``static_dir`` served from ``/states``.
    """

    static_path = Path(static_dir) if static_dir is not None else packaged_ui_dist()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        connect_config = ClientConfig.load_client_connect_config()
        app.state.temporal = await Client.connect(
            **connect_config,
            data_converter=await with_large_payload_offload(pydantic_data_converter),
        )

        resolved_registry = _resolve_registry(registry, registry_path)

        app.state.manager_handle = await _ensure_session_manager_workflow(
            app.state.temporal,
            registry=resolved_registry,
            manager_workflow_id=manager_workflow_id,
            manager_task_queue=manager_task_queue,
        )
        yield

    app = FastAPI(lifespan=lifespan)

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
            return JSONResponse({"status": "ok", "service": "temporal-agent-harness"})

    @app.get("/api/agents")
    async def list_agents():
        registry_result: AgentRegistry = await app.state.manager_handle.query(
            SessionManagerWorkflow.available_agents,
            result_type=AgentRegistry,
        )
        return asdict(registry_result)

    @app.get("/api/sessions")
    async def list_sessions():
        registry_result: AgentRegistry = await app.state.manager_handle.query(
            SessionManagerWorkflow.available_agents,
            result_type=AgentRegistry,
        )
        sessions: list[Session] = await app.state.manager_handle.query(
            SessionManagerWorkflow.list_sessions,
            result_type=list[Session],
        )
        known_workflow_ids = {session.workflow_id for session in sessions}
        try:
            discovered = await _discover_untracked_sessions(
                app.state.temporal, registry_result, known_workflow_ids
            )
        except Exception:  # noqa: BLE001 — a scan we cannot run is not worth the sessions we have
            # Discovery is an extra: it finds agent workflows this manager did not start. A
            # visibility outage must therefore cost the sessions it would have ADDED, not the
            # tracked ones the manager query already returned successfully.
            discovered = []
        return await _sessions_with_execution_state(
            app.state.temporal, sessions + discovered
        )

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequestBody):
        session: Session = await app.state.manager_handle.execute_update(
            SessionManagerWorkflow.create_session,
            ManagerCreateSessionRequest(
                agent_workflow_type=req.agent_workflow_type,
                config=AgentConfig(
                    is_message_queuing_enabled=req.is_message_queuing_enabled
                ),
            ),
            result_type=Session,
        )
        return await _session_with_execution_state(app.state.temporal, session)

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

    @app.get("/api/operator-interface/{session_id}")
    async def operator_interface(session_id: str):
        client = AgentClient(temporal=app.state.temporal, workflow_id=session_id)
        commands = await client.get_operator_interface()
        content = TypeAdapter(list[OperatorCommand]).dump_python(commands, mode="json")
        return JSONResponse(content=content, headers={"Cache-Control": "no-store"})

    @app.get("/api/attach")
    async def attach(session_id: str, from_offset: int = 0) -> StreamingResponse:
        client = AgentClient(temporal=app.state.temporal, workflow_id=session_id)

        async def events():
            """Stream the session, and say out loud every way that can fail.

            Once the response has begun there is no status code left to answer with, so a
            raise out of here ends the body with no frames at all — a stream that opens,
            says nothing and closes, which a browser cannot tell apart from a session that
            genuinely had nothing to send. Every failure is therefore reported IN BAND, as
            an ``error`` frame carrying ``kind``/``code`` and no ``type``: the shape a
            consumer reads as a fact about the connection rather than about the run.

            This is the same job the merge already does for a dying CHILD, whose
            ``subagent_stream_unavailable`` marker even says "refresh to retry"
            (``stream_merge/merge.py``). The root got no such courtesy.
            """
            delivered = 0
            try:
                stream = await client.attach(on_item=_yield_item, from_offset=from_offset)
                async for chunk in stream:
                    if chunk:
                        delivered += 1
                        yield chunk
            except (RPCError, WorkflowQueryFailedError) as exc:
                yield _sse(AgentEventType.ERROR, _attach_error(session_id, exc))
            else:
                # Ran to its end without raising and said nothing. On a RUNNING session
                # that is the ordinary caught-up attach; on a closed one it is the same
                # pathology as the NOT_FOUND branch minus the exception that made it
                # reportable — the merge's root cursor dies and ``merge.py`` ends the
                # generator with a bare ``return``, so a run of hundreds of events
                # arrives as silence.
                if delivered == 0:
                    chunk = await _unreplayable_run_frame(
                        app.state.temporal, session_id, from_offset=from_offset
                    )
                    if chunk:
                        yield chunk

        return StreamingResponse(
            events(),
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

    @app.post("/api/operator-commands")
    async def execute_operator_command(req: OperatorCommandRequestBody):
        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        result = await client.execute_operator_command(req.name, arg=req.arg)
        content = TypeAdapter(OperatorCommandResult).dump_python(result, mode="json")
        return JSONResponse(content=content, headers={"Cache-Control": "no-store"})

    @app.post("/api/messages")
    async def submit_message(req: ChatRequestBody):
        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        if isinstance(req.message, str):
            msg_type, payload = "ask", {"text": req.message}
        else:
            msg_type, payload = req.message["type"], req.message.get("payload") or {}

        result = await client.submit_message(msg_type, payload, req.expected_turn)
        return JSONResponse(content=asdict(result), headers={"Cache-Control": "no-store"})

    @app.post("/api/chat")
    async def chat(req: ChatRequestBody):
        def on_item(item: AgentStreamOutput, position: StreamPosition) -> bytes:
            match item:
                case AgentTurnTimeout():
                    return _sse(
                        AgentEventType.ERROR,
                        {"kind": "timeout", "message": str(item)},
                        position,
                    )
                case AgentTurnError():
                    return _sse(
                        AgentEventType.ERROR,
                        {"kind": "agent", "message": str(item)},
                        position,
                    )
                case _:
                    return _yield_item(item, position)

        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        if isinstance(req.message, str):
            msg_type, payload = "ask", {"text": req.message}
        else:
            msg_type, payload = req.message["type"], req.message.get("payload") or {}

        return StreamingResponse(
            await client.send_message(
                msg_type,
                payload,
                req.expected_turn,
                on_item=on_item,
            ),
            media_type="text/event-stream",
            headers=_sse_headers(),
        )

    @app.exception_handler(StaleTurnError)
    async def stale_turn_handler(request, exc):
        return JSONResponse(
            status_code=409,
            content={"error": "stale_turn", "message": str(exc)},
        )

    @app.exception_handler(AgentBusyError)
    async def agent_busy_handler(request, exc):
        return JSONResponse(
            status_code=409,
            content={"error": "agent_busy", "message": str(exc)},
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

    @app.exception_handler(CallbackResultError)
    async def callback_result_handler(request, exc):
        return JSONResponse(
            status_code=409,
            content={
                "error": exc.error_type or "callback_result_error",
                "message": str(exc),
            },
        )

    @app.exception_handler(RPCError)
    async def rpc_error_handler(request, exc: RPCError):
        """A workflow that is gone is a missing resource, not a server fault.

        The session registry outlives the workflows it lists, and Temporal
        retention eventually drops closed ones, so any endpoint that takes a
        workflow id can be handed a dead one. Answering 500 with a traceback
        made an expected condition look like a bug and buried real ones. Handled
        here rather than per-endpoint so every route that queries a workflow
        agrees, including ones added later.

        Anything other than NOT_FOUND is a genuine fault and is left to
        propagate, keeping its 500 and its traceback.
        """
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise exc
        return JSONResponse(
            status_code=404,
            content={"error": "workflow_not_found", "message": str(exc)},
        )

    return app


def _resolve_registry(
    registry: RegistrySource | None,
    registry_path: Path | str | None,
) -> AgentRegistry:
    if registry is not None and registry_path is not None:
        raise ValueError("Pass either registry or registry_path, not both.")
    if isinstance(registry, AgentRegistry):
        return registry
    if callable(registry):
        return registry()
    if registry_path is not None:
        return load_agent_registry(registry_path)
    raise ValueError("create_agent_harness_app requires registry or registry_path.")


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


async def _session_with_execution_state(
    temporal: Client,
    session: Session,
) -> dict[str, object]:
    state, initial_user_message = await asyncio.gather(
        _workflow_execution_state(temporal, session.workflow_id),
        _session_initial_user_message(temporal, session.workflow_id),
    )
    content = {**asdict(session), **state}
    if initial_user_message is not None:
        content["initial_user_message"] = initial_user_message
    return content


_DISCOVERY_LIMIT = 200


async def _discover_untracked_sessions(
    temporal: Client,
    registry: AgentRegistry,
    known_workflow_ids: set[str],
) -> list[Session]:
    """Find agent workflows already running in the namespace that this session manager didn't
    start itself (e.g. launched directly against a worker, or by another session manager),
    so the UI can list and attach to them instead of only ones it created via ``create_session``.
    """

    if not registry.agents:
        return []

    escaped_types = [agent.workflow_type.replace("'", "''") for agent in registry.agents]
    types_filter = " OR ".join(f"WorkflowType='{workflow_type}'" for workflow_type in escaped_types)
    query = f"ExecutionStatus='Running' AND ({types_filter})"

    discovered: list[Session] = []
    async for execution in temporal.list_workflows(query=query, limit=_DISCOVERY_LIMIT):
        if execution.id in known_workflow_ids:
            continue
        descriptor = registry.by_workflow_type(execution.workflow_type)
        if descriptor is None:
            continue
        discovered.append(
            Session(
                workflow_id=execution.id,
                created_at=execution.start_time.timestamp(),
                label=descriptor.label,
                agent_workflow_type=execution.workflow_type,
                is_discovered=True,
            )
        )
    return discovered


async def _sessions_with_execution_state(
    temporal: Client,
    sessions: list[Session],
) -> list[dict[str, object]]:
    """Enrich every session, and let each one fail on its own.

    Forty concurrent describes against one dev-server frontend is a place timeouts are the
    expected case, not a theoretical one — the console's own poller records twelve seconds
    against twenty stale entries, on a ten-second interval. Without ``return_exceptions`` the
    first of those took the whole list with it and every healthy session vanished from the
    sidebar, which is a far worse answer than one row whose status is momentarily unknown.
    """
    states = await asyncio.gather(
        *(_session_with_execution_state(temporal, session) for session in sessions),
        return_exceptions=True,
    )
    return [
        _unknown_execution_state(session) if isinstance(state, BaseException) else state
        for session, state in zip(sessions, states, strict=True)
    ]


def _unknown_execution_state(session: Session) -> dict[str, object]:
    """One session the describe could not answer for: listed, with its status withheld.

    ``closed`` stays false because the failure says nothing about the workflow — claiming a
    live session had ended would be a worse lie than admitting to not knowing, and the next
    poll corrects it either way.
    """
    return {**asdict(session), "execution_status": "UNKNOWN", "closed": False}


def _attach_error(
    session_id: str, exc: RPCError | WorkflowQueryFailedError
) -> dict[str, str]:
    """The in-band ``error`` payload for a failure while opening or reading a stream.

    ``NOT_FOUND`` is the commonest attach failure a dev stack has — the session list outlives
    the executions in it, so every id from before a namespace's retention window points at
    nothing — and it gets its own ``code`` because it is the one case where retrying is
    pointless: there is no history left to ask for. Everything else shares
    ``stream_unavailable`` and names its cause in the message, since what a consumer does
    about a timeout, an absent worker or a query a worker refused is the same thing.
    """
    if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
        return {
            "kind": "unavailable",
            "code": "workflow_not_found",
            "message": (
                f"No workflow {session_id!r} in this namespace. Its history has been "
                "deleted or has passed the namespace's retention, so there is nothing "
                "left to stream."
            ),
        }
    cause = exc.status.name if isinstance(exc, RPCError) else str(exc)
    return {
        "kind": "unavailable",
        "code": "stream_unavailable",
        "message": (
            f"The event stream for session {session_id!r} could not be read ({cause}). "
            "Check that Temporal is reachable and that a worker is polling this agent's "
            "task queue, then refresh to retry."
        ),
    }


async def _unreplayable_run_frame(
    temporal: Client,
    session_id: str,
    *,
    from_offset: int,
) -> bytes | None:
    """The frame for an attach that opened on a finished run and read nothing.

    ``unreplayable_run`` rather than ``workflow_not_found``: Temporal HAS this history — the
    run is in the workflow list and its events are in the history UI — so telling the reader
    the session is gone would be false.

    ``None`` when the silence is honest, which is most of the time: a RUNNING session the
    reader has caught up with streams nothing every time it is attached to, and so does a
    closed run whose whole log the reader already holds.
    """
    handle = temporal.get_workflow_handle(session_id)
    try:
        desc = await handle.describe()
    except RPCError:
        # The status is the only thing that makes this reportable, so an unreachable
        # Temporal leaves the stream as quiet as it was before.
        return None
    if desc.status == WorkflowExecutionStatus.RUNNING:
        return None

    published = await _published_event_count(temporal, session_id)
    if published is not None and published <= from_offset:
        return None

    counted = (
        f" its {published} recorded events"
        if published is not None
        else " the events it recorded"
    )
    return _sse(
        AgentEventType.ERROR,
        {
            "kind": "unavailable",
            "code": "unreplayable_run",
            "message": (
                f"Session {session_id!r} finished ({desc.status.name}) and its event "
                f"stream cannot be replayed, so none of{counted} could be read. The "
                "history itself is intact in Temporal; what cannot be rebuilt is this "
                "console's transcript of it."
            ),
        },
    )


async def _published_event_count(temporal: Client, session_id: str) -> int | None:
    """How many events this session published, or ``None`` if it cannot say.

    It is what tells "this run published nothing" apart from "this run published 394 events
    and none of them could be read", and it costs a history replay on the worker — so it is
    only ever asked on the path where the alternative is a silent stream.
    """
    try:
        return await WorkflowStreamClient.create(temporal, session_id).get_offset()
    except Exception:  # noqa: BLE001 — a count we cannot take is not worth a silent stream
        return None


async def _session_initial_user_message(
    temporal: Client,
    workflow_id: str,
) -> str | None:
    handle = temporal.get_workflow_handle(workflow_id)
    scanned_events = 0
    try:
        async for event in handle.fetch_history_events(
            page_size=_SESSION_PREVIEW_HISTORY_PAGE_SIZE,
            wait_new_event=False,
            rpc_timeout=_SESSION_PREVIEW_HISTORY_RPC_TIMEOUT,
        ):
            scanned_events += 1
            user_message = await _session_user_message_from_history_event(
                temporal,
                event,
            )
            if user_message is not None:
                return _display_user_message(user_message.model_dump_json())
            if scanned_events >= _SESSION_PREVIEW_HISTORY_MAX_EVENTS:
                break
    except Exception:
        return None
    return None


async def _session_user_message_from_history_event(
    temporal: Client,
    event: HistoryEvent,
) -> AgentMessage | None:
    if event.event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED:
        return None
    if not event.HasField("workflow_execution_update_accepted_event_attributes"):
        return None

    request = (
        event.workflow_execution_update_accepted_event_attributes.accepted_request
    )
    if request.input.name != SEND_AGENT_MESSAGE_UPDATE:
        return None
    if not request.input.args.payloads:
        return None

    try:
        decoded = await temporal.data_converter.decode(
            request.input.args.payloads,
            [AgentMessage],
        )
    except Exception:
        return None
    if not decoded or not isinstance(decoded[0], AgentMessage):
        return None
    return decoded[0]


def _display_user_message(value: str) -> str:
    if not value.startswith("{"):
        return value
    try:
        message = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(message, dict):
        return value

    payload = message.get("payload")
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text
        script = payload.get("script")
        if isinstance(script, str):
            return script
        name = payload.get("name")
        arg = payload.get("arg")
        if isinstance(name, str) and message.get("type") in {"slash", "slash_command"}:
            display_name = "model" if name == "set-model" else name
            return f"/{display_name}{f' {arg}' if isinstance(arg, str) and arg else ''}"

    script = message.get("script")
    if isinstance(script, str):
        return script
    return value


async def _ensure_session_manager_workflow(
    temporal: Client,
    *,
    registry: AgentRegistry,
    manager_workflow_id: str,
    manager_task_queue: str,
) -> WorkflowHandle[Any, Any]:
    handle = temporal.get_workflow_handle(manager_workflow_id)
    try:
        desc = await handle.describe()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    else:
        if desc.status == WorkflowExecutionStatus.RUNNING:
            print(f"Connected to existing session manager: {manager_workflow_id}")
            return handle
        print(
            "Existing session manager "
            f"{manager_workflow_id} is {desc.status.name}; starting new run"
        )

    try:
        handle = await temporal.start_workflow(
            SessionManagerWorkflow.run,
            registry,
            id=manager_workflow_id,
            task_queue=manager_task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
    except WorkflowAlreadyStartedError:
        handle = temporal.get_workflow_handle(manager_workflow_id)
        print(
            "Connected to session manager started concurrently: "
            f"{manager_workflow_id}"
        )
    else:
        print(f"Ensured session manager is running: {manager_workflow_id}")
    return handle


def _mount_static_ui(
    app: FastAPI,
    *,
    static_path: Path,
    index_file: str,
    states_file: str | None,
) -> None:
    if not static_path.exists():
        raise ValueError(f"Static UI directory does not exist: {static_path}")

    assets_path = static_path / "assets"
    if assets_path.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_path), name="assets")
    app.mount("/static", StaticFiles(directory=static_path), name="static")

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


def _sse(event: str, data: dict, position: StreamPosition | None = None) -> bytes:
    """Frame one SSE event, stamping both of the position's offsets when there is one.

    The two are for different jobs and a client needs both: ``resume_offset`` is what it hands back
    to ``attach(from_offset=...)`` to resume, while ``event_offset`` with the envelope's ``agent_id``
    is what IDENTIFIES this event — stable across redeliveries and distinct between the events of a
    single subagent turn, which the resume cursor is not (see
    :class:`~temporal_agent_harness.harness.stream_merge.StreamPosition`).

    ``replay`` goes on the wire only when true, so its absence means live — which is also what an
    older server means by not sending it at all, and what the per-turn ``/api/chat`` path means by
    never being a catch-up in the first place."""
    payload = {**data}
    if position is not None:
        payload["resume_offset"] = position.resume_offset
        payload["event_offset"] = position.event_offset
        if position.replay:
            payload["replay"] = True
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def _yield_item(item, position: StreamPosition | None = None) -> bytes:
    if isinstance(item, AgentEvent):
        payload = item.event
        data = {
            **payload.model_dump(mode="json"),
            "agent_id": item.agent_id,
            "turn_id": item.turn_id,
            "turn_number": item.turn_number,
            "timestamp": item.timestamp,
        }
        return _sse(payload.type, data, position)
    return b""


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
