# ABOUTME: FastAPI server that bridges the AgentClient to a browser chat UI
# via SSE streaming. Thin layer — all agent logic lives in agent_client.py.
#
# The server connects to (or creates) a SessionManagerWorkflow which owns
# all agent session lifecycle. No mutable server-side state beyond the
# manager's workflow ID.

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.envconfig import ClientConfig

from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentEvent,
    AgentEventType,
)
from temporal_agent_harness.harness.agent_client import (
    AgentBusyError,
    AgentClient,
    AgentStreamOutput,
    AgentTurnError,
    AgentTurnTimeout,
    StaleTurnError,
    ToolApprovalError,
)
from examples.session_manager.agent_registry import load_agent_registry
from examples.session_manager.workflow import (
    SESSION_MANAGER_ID,
    SESSION_MANAGER_TASK_QUEUE,
    AgentRegistry,
    Session,
    SessionManagerWorkflow,
)
from examples.session_manager.workflow import (
    CreateSessionRequest as ManagerCreateSessionRequest,
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Match the large-payload codec the agent workers use so any offloaded payload
    # (e.g. a large tool result or reply) can be read back (see large_payload).
    connect_config = ClientConfig.load_client_connect_config()
    app.state.temporal = await Client.connect(
        **connect_config,
        data_converter=await with_large_payload_offload(pydantic_data_converter),
    )

    # The agent roster (examples/session_manager/agents.toml). We read it here — the server is one of the
    # processes that starts the manager — and pass it in as the manager's init arg. The
    # manager then serves it back over `available_agents` (exposed at /api/agents), so the
    # endpoints query the manager rather than this copy.
    registry = load_agent_registry()

    # Connect to existing running manager, or start a fresh one.
    from temporalio.client import WorkflowExecutionStatus

    need_new = True
    try:
        handle = app.state.temporal.get_workflow_handle(SESSION_MANAGER_ID)
        desc = await handle.describe()
        if desc.status == WorkflowExecutionStatus.RUNNING:
            need_new = False
            print(f"Connected to existing session manager: {SESSION_MANAGER_ID}")
    except Exception:
        pass

    if need_new:
        await app.state.temporal.start_workflow(
            SessionManagerWorkflow.run,
            registry,
            id=SESSION_MANAGER_ID,
            task_queue=SESSION_MANAGER_TASK_QUEUE,
        )
        handle = app.state.temporal.get_workflow_handle(SESSION_MANAGER_ID)
        print(f"Started new session manager: {SESSION_MANAGER_ID}")

    app.state.manager_handle = handle  # type: ignore
    yield


app = FastAPI(lifespan=lifespan)

# Serve vendored static assets (e.g. the Temporal logo used in the chat UI's
# model-activity indicator) from server/static at /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class CreateSessionRequest(BaseModel):
    # Which agent to launch, by registered ``@workflow.defn`` name. REQUIRED — the server
    # picks no default; the client (the UI's agent picker) always sends an explicit choice
    # whenever a new session is desired. The manager validates it against the registry.
    agent_workflow_type: str
    is_message_queuing_enabled: bool = False


class ChatRequest(BaseModel):
    session_id: str
    # Plain text, or the JSON object of a typed message the agent accepts — a
    # {"type": <handler-name>, "payload": {...}} envelope naming one of the agent's
    # non-text @agent.accepts handlers.
    message: str | dict[str, Any]
    expected_turn: int
    from_offset: int = 0


class ToolApprovalRequestBody(BaseModel):
    session_id: str
    tool_id: str
    approved: bool
    reason: str | None = None
    # "Approve, and stop asking me about this tool": allow-lists the tool on the agent's
    # live ToolApprovalPolicy so future calls of it skip the gate (and any other pending
    # call of it auto-resolves). Only meaningful with approved=True.
    remember: bool = False


def _sse(event: str, data: dict, offset: int | None = None) -> bytes:
    payload = {**data}
    if offset is not None:
        payload["offset"] = offset
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def _yield_item(item, offset: int | None = None) -> bytes:
    # Every stream item is an AgentEvent envelope. Flatten it for the wire: the
    # SSE event name is the nested payload's ``type``, and the data is the
    # payload's fields plus the envelope's routing metadata (turn_id /
    # turn_number / timestamp), keeping the browser-facing payload flat.
    if isinstance(item, AgentEvent):
        payload = item.event
        data = {
            **payload.model_dump(mode="json"),
            "turn_id": item.turn_id,
            "turn_number": item.turn_number,
            "timestamp": item.timestamp,
        }
        return _sse(payload.type, data, offset)
    return b""


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/states")
async def states():
    """Real-time state-machine diagram view over the same agent event stream."""
    return FileResponse(STATIC_DIR / "states.html")


@app.get("/api/agents")
async def list_agents():
    """The roster of launchable agents, for the UI's agent picker.

    Proxies the manager's ``available_agents`` query — the manager is the source of truth
    (it was started with the registry from examples/session_manager/agents.toml). Returns the agents and
    which one is the default, so the front end can populate a picker without hardcoding
    the agent list.
    """
    registry: AgentRegistry = await app.state.manager_handle.query(
        SessionManagerWorkflow.available_agents,
        result_type=AgentRegistry,
    )
    return asdict(registry)


@app.get("/api/sessions")
async def list_sessions():
    """List all agent sessions from the manager workflow."""
    sessions: list[Session] = await app.state.manager_handle.query(
        SessionManagerWorkflow.list_sessions,
        result_type=list[Session],
    )
    return [asdict(s) for s in sessions]


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest):
    """Create a new agent session via the manager workflow.

    The client always names which agent to launch (``agent_workflow_type``, discovered via
    ``/api/agents``); the manager validates it against the registry and resolves where that
    agent runs, so the server hardcodes neither the agent nor its task queue.
    """
    session: Session = await app.state.manager_handle.execute_update(
        SessionManagerWorkflow.create_session,
        ManagerCreateSessionRequest(
            agent_workflow_type=req.agent_workflow_type,
            config=AgentConfig(
                is_message_queuing_enabled=req.is_message_queuing_enabled
            ),
            # task_queue omitted — the manager derives it from the registry.
        ),
        result_type=Session,
    )
    return asdict(session)


@app.get("/api/status/{session_id}")
async def get_status(session_id: str):
    """Get the current agent status for a session."""
    client = AgentClient(
        temporal=app.state.temporal,
        workflow_id=session_id,
    )
    status = await client.get_status()
    return JSONResponse(
        content=asdict(status),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/agent-interface/{session_id}")
async def agent_interface(session_id: str):
    """Return this agent's callable surface — its ``@agent.accepts`` handlers, tool-style.

    Proxies the workflow's discovery query. The contract is the same for every session of
    a given agent type, so the browser fetches it once and derives its message-construction
    UI (e.g. slash commands) from it — letting the agent's functions evolve without
    front-end changes. Each entry has ``name`` / ``description`` / ``parameters`` (input
    JSON schema) / ``output`` (output JSON schema).
    """
    client = AgentClient(
        temporal=app.state.temporal,
        workflow_id=session_id,
    )
    functions = await client.get_agent_interface()
    return JSONResponse(content=[fn.model_dump(mode="json") for fn in functions])


@app.get("/api/attach")
async def attach(session_id: str, from_offset: int = 0) -> StreamingResponse:
    """Reattach to a session's event stream."""
    client = AgentClient(
        temporal=app.state.temporal,
        workflow_id=session_id,
    )
    return StreamingResponse(
        client.attach(on_item=_yield_item, from_offset=from_offset),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/api/approve")
async def approve_tool(req: ToolApprovalRequestBody):
    """Resolve a pending human-in-the-loop tool approval.

    The gated tool call is parked in-workflow awaiting this decision (a
    ``tool_approval_requested`` event was streamed with ``tool_id``). On approval the
    call dispatches; on denial the model gets ``reason`` as the tool's error result.
    Either way the workflow then streams the follow-on events (``tool_approval_resolved``,
    then ``tool_start``/``tool_end`` on approval) over the in-flight ``/api/chat`` stream.
    A decision for an unknown or already-resolved ``tool_id`` is a 409.
    """
    client = AgentClient(
        temporal=app.state.temporal,
        workflow_id=req.session_id,
    )
    result = await client.approve_tool(
        req.tool_id, approved=req.approved, reason=req.reason, remember=req.remember
    )
    return JSONResponse(content=asdict(result), headers={"Cache-Control": "no-store"})


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Send a message to a session and stream back ALL events.

    The stream includes events from ``from_offset`` through to the
    completion of the submitted turn — including any intermediate turns
    from other clients or queued messages. This lets the caller drop a
    prior stream and resume seamlessly.

    Phase 1 (the workflow update) happens eagerly — StaleTurnError and
    AgentBusyError propagate as HTTP errors via the exception handlers.
    """
    def on_item(item: AgentStreamOutput, offset: int) -> bytes:
        match item:
            case AgentTurnTimeout():
                return _sse(
                    AgentEventType.ERROR, {"kind": "timeout", "message": str(item)}, offset
                )
            case AgentTurnError():
                return _sse(
                    AgentEventType.ERROR, {"kind": "agent", "message": str(item)}, offset
                )
            case _:
                return _yield_item(item, offset)

    client = AgentClient(
        temporal=app.state.temporal,
        workflow_id=req.session_id,
    )

    # The browser sends free text as a bare string and a typed message as a ready
    # ``{type, payload}`` dict. Resolve (function, payload) either way: plain text targets
    # the agent's free-text handler (``ask``, taking a ``TextMessage``) — the conversational
    # agents this chat surface targets expose that handler, so its name is known here, not guessed.
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
            from_offset=req.from_offset,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Exception handlers — pre-stream errors become normal HTTP responses
# ---------------------------------------------------------------------------


@app.exception_handler(StaleTurnError)
async def stale_turn_handler(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409,
        content={"error": "stale_turn", "message": str(exc)},
    )


@app.exception_handler(AgentBusyError)
async def agent_busy_handler(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=409,
        content={"error": "agent_busy", "message": str(exc)},
    )


@app.exception_handler(ToolApprovalError)
async def tool_approval_handler(request, exc):
    # Unknown tool_id, or an approval already resolved (idempotency guard) — the
    # frontend uses ``error`` to decide whether to re-enable its approve/deny buttons.
    return JSONResponse(
        status_code=409,
        content={"error": exc.error_type or "tool_approval_error", "message": str(exc)},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
