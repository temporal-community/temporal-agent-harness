"""FastAPI app factory for the harness web UI/API."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, TypeAdapter
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.harness.agent_client import (
    AgentBusyError,
    AgentClient,
    AgentStreamOutput,
    AgentTurnError,
    AgentTurnTimeout,
    StaleTurnError,
    ToolApprovalError,
)
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentStatus,
)
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

        from temporalio.client import WorkflowExecutionStatus

        need_new = True
        try:
            handle = app.state.temporal.get_workflow_handle(manager_workflow_id)
            desc = await handle.describe()
            if desc.status == WorkflowExecutionStatus.RUNNING:
                need_new = False
                print(f"Connected to existing session manager: {manager_workflow_id}")
        except Exception:
            pass

        if need_new:
            await app.state.temporal.start_workflow(
                SessionManagerWorkflow.run,
                resolved_registry,
                id=manager_workflow_id,
                task_queue=manager_task_queue,
            )
            handle = app.state.temporal.get_workflow_handle(manager_workflow_id)
            print(f"Started new session manager: {manager_workflow_id}")

        app.state.manager_handle = handle
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
        return [asdict(session) for session in sessions]

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
        return asdict(session)

    @app.get("/api/status/{session_id}")
    async def get_status(session_id: str):
        client = AgentClient(temporal=app.state.temporal, workflow_id=session_id)
        status = await client.get_status()
        content = TypeAdapter(AgentStatus).dump_python(status, mode="json")
        return JSONResponse(content=content, headers={"Cache-Control": "no-store"})

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
