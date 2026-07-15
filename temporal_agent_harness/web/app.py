"""FastAPI app factory for the harness web UI/API."""

from __future__ import annotations

import asyncio
import json
import os
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
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
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
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentStatus,
    OperatorCommand,
    OperatorCommandResult,
)
from temporal_agent_harness.ui import packaged_ui_dist
from temporal_agent_harness.utils.large_payload import with_large_payload_offload
from temporal_agent_harness.web.registry import load_agent_registry
from temporal_agent_harness.web.session_manager import (
    SESSION_MANAGER_ID,
    SESSION_MANAGER_TASK_QUEUE,
    AgentRegistry,
)
from temporal_agent_harness.web.session_manager import (
    CreateSessionRequest as ManagerCreateSessionRequest,
)
from temporal_agent_harness.web.session_manager import Session, SessionManagerWorkflow

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
        if not connect_config.get("target_host"):
            connect_config["target_host"] = os.environ.get(
                "TEMPORAL_ADDRESS", "localhost:7233"
            )
        if not connect_config.get("namespace"):
            connect_config["namespace"] = os.environ.get(
                "TEMPORAL_NAMESPACE", "default"
            )
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
        sessions: list[Session] = await app.state.manager_handle.query(
            SessionManagerWorkflow.list_sessions,
            result_type=list[Session],
        )
        return await _sessions_with_execution_state(app.state.temporal, sessions)

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
        return JSONResponse(
            content=asdict(result), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/callback-result")
    async def provide_callback_result(req: CallbackResultRequestBody):
        """Fulfill a pending callback tool call: a client that executed the tool on its own
        machine submits the result (or an error), keyed by the ``tool_id`` from the
        ``callback_requested`` event. Forwards to the workflow's ``provide_callback_result``
        update; the result is validated against the tool's declared output type there.
        """
        client = AgentClient(temporal=app.state.temporal, workflow_id=req.session_id)
        result = await client.provide_callback_result(
            req.tool_id, result=req.result, error=req.error
        )
        return JSONResponse(
            content=asdict(result), headers={"Cache-Control": "no-store"}
        )

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
        return JSONResponse(
            content=asdict(result), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/chat")
    async def chat(req: ChatRequestBody):
        def on_item(item: AgentStreamOutput, resume_offset: int) -> bytes:
            match item:
                case AgentTurnTimeout():
                    return _sse(
                        AgentEventType.ERROR,
                        {"kind": "timeout", "message": str(item)},
                        resume_offset,
                    )
                case AgentTurnError():
                    return _sse(
                        AgentEventType.ERROR,
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


async def _sessions_with_execution_state(
    temporal: Client,
    sessions: list[Session],
) -> list[dict[str, object]]:
    return list(
        await asyncio.gather(
            *(_session_with_execution_state(temporal, session) for session in sessions)
        )
    )


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

    request = event.workflow_execution_update_accepted_event_attributes.accepted_request
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
            f"Connected to session manager started concurrently: {manager_workflow_id}"
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
