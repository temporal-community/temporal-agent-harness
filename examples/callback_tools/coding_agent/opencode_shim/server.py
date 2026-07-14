"""Standalone OpenCode-protocol server.

Implements the endpoint surface the OpenCode TUI needs to boot and chat,
backed by an `AgentBackend` (see backend.py). Point the TUI at it with:

    opencode --attach http://127.0.0.1:4096   # or OPENCODE_API_URL

Protocol shape derived from OpenCode's server API (and AgentPool's
compatibility implementation). Pin your opencode version; this is an
internal protocol, not a formal standard.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from . import _identifiers as identifier
from . import local_tools
from ._time import now_ms
from .backend import AgentBackend, AgentTurn
from .models import (
    Agent,
    AnyMessageWithParts,
    App,
    AppTimeInfo,
    Config,
    FileContent,
    FileDiff,
    FileNode,
    FindMatch,
    HealthResponse,
    MessageRequest,
    MessageUpdatedEvent,
    MessageWithParts,
    Model,
    ModelCost,
    ModelLimit,
    ModelRef,
    PartUpdatedEvent,
    PathInfo,
    PermissionAskedProperties,
    PermissionReplyRequest,
    PermissionResolvedEvent,
    Project,
    ProjectTime,
    Provider,
    ProviderListResponse,
    ProvidersResponse,
    ServerConnectedEvent,
    ServerHeartbeatEvent,
    Session,
    SessionCreatedEvent,
    SessionCreateRequest,
    SessionDeletedEvent,
    SessionStatus,
    SessionUpdatedEvent,
    SessionUpdateRequest,
    SubmatchInfo,
    TextPartInput,
    TimeCreated,
    TimeCreatedUpdated,
    Todo,
    UserMessage,
    VcsInfo,
)
from .state import ShimState

VERSION = "0.1.0"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".ruff_cache", "dist", "build", ".next", "target"}

PROVIDER_ID = "temporal"
MODEL_ID = "agent"


class OpenCodeJSONResponse(JSONResponse):
    """Serialize with camelCase aliases and drop None (matches OpenCode)."""

    def render(self, content: Any) -> bytes:
        return super().render(jsonable_encoder(content, exclude_none=True, by_alias=True))


def create_app(*, backend: AgentBackend, working_dir: str | None = None) -> FastAPI:
    state = ShimState(working_dir=str(Path(working_dir or os.getcwd()).resolve()))
    app = FastAPI(title="opencode-temporal-shim", version=VERSION, default_response_class=OpenCodeJSONResponse)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
    )
    app.state.shim = state
    config = Config()

    # ------------------------------------------------------------------ utils

    def get_session(session_id: str) -> Session:
        session = state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    def touch(session: Session) -> None:
        session.time.updated = now_ms()

    def resolve_path(rel: str) -> Path:
        root = Path(state.working_dir)
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise HTTPException(status_code=400, detail="Path escapes working directory")
        return target

    # ------------------------------------------------------------ event stream

    def serialize_event(event: Any, *, wrap: bool) -> str:
        data = event.model_dump(by_alias=True, exclude_none=True)
        return json.dumps({"payload": data} if wrap else data)

    async def event_stream(*, wrap: bool) -> AsyncGenerator[str, None]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        state.event_subscribers.append(queue)
        try:
            yield f"data: {serialize_event(ServerConnectedEvent(), wrap=wrap)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10.0)
                except TimeoutError:
                    yield f"data: {serialize_event(ServerHeartbeatEvent(), wrap=wrap)}\n\n"
                    continue
                yield f"data: {serialize_event(event, wrap=wrap)}\n\n"
        finally:
            state.event_subscribers.remove(queue)

    def sse(gen: AsyncGenerator[str, None]) -> StreamingResponse:
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/event")
    async def get_events() -> StreamingResponse:
        return sse(event_stream(wrap=False))

    @app.get("/global/event")
    async def get_global_events() -> StreamingResponse:
        return sse(event_stream(wrap=True))

    # ---------------------------------------------------------------- global

    @app.get("/global/health")
    async def health() -> HealthResponse:
        return HealthResponse(healthy=True, version=VERSION)

    @app.get("/global/config")
    @app.get("/config")
    async def get_config() -> Config:
        return config

    @app.patch("/global/config")
    @app.patch("/config")
    async def patch_config(body: Config) -> Config:
        nonlocal config
        config = body
        return config

    @app.post("/global/dispose")
    @app.post("/instance/dispose")
    async def dispose() -> bool:
        return True

    @app.post("/global/upgrade")
    async def upgrade() -> dict[str, Any]:
        return {"success": False, "error": "Not applicable"}

    @app.post("/log")
    async def post_log(body: dict[str, Any]) -> bool:
        return True

    # ------------------------------------------------------- providers/agents

    def providers() -> list[Provider]:
        model = Model(
            id=MODEL_ID,
            name="Temporal Agent",
            cost=ModelCost(input=0, output=0),
            limit=ModelLimit(context=200_000, output=64_000),
            release_date="2026-01-01",
        )
        return [Provider(id=PROVIDER_ID, name="Temporal", env=[], models={MODEL_ID: model})]

    @app.get("/config/providers")
    async def get_providers() -> ProvidersResponse:
        return ProvidersResponse(providers=providers(), default={PROVIDER_ID: MODEL_ID})

    @app.get("/provider")
    async def list_providers() -> ProviderListResponse:
        return ProviderListResponse(all=providers(), default={PROVIDER_ID: MODEL_ID}, connected=[PROVIDER_ID])

    @app.get("/agent")
    async def list_agents() -> list[Agent]:
        return [
            Agent(
                name="build",
                description="Temporal-backed agent",
                mode="primary",
                default=True,
                model=ModelRef(provider_id=PROVIDER_ID, model_id=MODEL_ID),
            )
        ]

    @app.get("/mode")
    async def list_modes() -> list[dict[str, Any]]:
        return [{"name": "build"}]

    @app.get("/command")
    async def list_commands() -> list[Any]:
        return []

    @app.get("/skill")
    async def list_skills() -> list[Any]:
        return []

    @app.get("/mcp")
    async def mcp_status() -> dict[str, Any]:
        return {}

    @app.get("/lsp")
    async def lsp_status() -> list[Any]:
        return []

    @app.get("/formatter")
    async def formatter_status() -> list[Any]:
        return []

    @app.get("/provider/auth")
    async def provider_auth() -> dict[str, Any]:
        return {}

    @app.get("/question/")
    @app.get("/question")
    async def list_questions() -> list[Any]:
        return []

    # ------------------------------------------------------------- app/project

    def path_info() -> PathInfo:
        return PathInfo.for_directory(state.working_dir)

    def current_project() -> Project:
        wd = Path(state.working_dir)
        is_git = (wd / ".git").is_dir()
        return Project(
            id=state.project_id,
            worktree=state.working_dir,
            vcs="git" if is_git else None,
            vcs_dir=str(wd / ".git") if is_git else None,
            time=ProjectTime(created=int(state.start_time * 1000)),
        )

    @app.get("/app")
    async def get_app_info() -> App:
        return App(
            git=(Path(state.working_dir) / ".git").is_dir(),
            hostname=socket.gethostname(),
            path=path_info(),
            time=AppTimeInfo(initialized=state.start_time),
        )

    @app.get("/path")
    async def get_path() -> PathInfo:
        return path_info()

    @app.get("/project")
    async def list_projects() -> list[Project]:
        return [current_project()]

    @app.get("/project/current")
    async def get_current_project() -> Project:
        return current_project()

    @app.get("/vcs")
    async def get_vcs() -> VcsInfo:
        git_dir = Path(state.working_dir) / ".git"
        if not git_dir.is_dir():
            return VcsInfo()

        async def run(*args: str) -> str | None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", *args, cwd=state.working_dir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await proc.communicate()
                return out.decode().strip() if proc.returncode == 0 else None
            except OSError:
                return None

        branch, commit, dirty = await asyncio.gather(
            run("rev-parse", "--abbrev-ref", "HEAD"),
            run("rev-parse", "HEAD"),
            run("status", "--porcelain"),
        )
        return VcsInfo(branch=branch, commit=commit, dirty=bool(dirty))

    # ---------------------------------------------------------------- sessions

    @app.get("/session")
    async def list_sessions(
        roots: bool | None = None, search: str | None = None, limit: int | None = None
    ) -> list[Session]:
        sessions = list(state.sessions.values())
        if roots:
            sessions = [s for s in sessions if s.parent_id is None]
        if search:
            sessions = [s for s in sessions if search.lower() in s.title.lower()]
        sessions.sort(key=lambda s: s.time.updated, reverse=True)
        return sessions[:limit] if limit else sessions

    @app.post("/session")
    async def create_session(body: SessionCreateRequest | None = None) -> Session:
        now = now_ms()
        session = Session(
            id=identifier.ascending("session"),
            project_id=state.project_id,
            directory=state.working_dir,
            title=(body.title if body and body.title else "New session"),
            parent_id=body.parent_id if body else None,
            time=TimeCreatedUpdated(created=now, updated=now),
        )
        state.sessions[session.id] = session
        state.messages[session.id] = []
        state.session_status[session.id] = SessionStatus(type="idle")
        await state.broadcast(SessionCreatedEvent.create(session))
        return session

    @app.get("/session/status")
    async def session_statuses() -> dict[str, SessionStatus]:
        return state.session_status

    @app.get("/session/{session_id}")
    async def get_session_route(session_id: str) -> Session:
        return get_session(session_id)

    @app.patch("/session/{session_id}")
    async def update_session(session_id: str, body: SessionUpdateRequest) -> Session:
        session = get_session(session_id)
        if body.title is not None:
            session.title = body.title
        touch(session)
        await state.broadcast(SessionUpdatedEvent.create(session))
        return session

    @app.delete("/session/{session_id}")
    async def delete_session(session_id: str) -> bool:
        session = get_session(session_id)
        task = state.running_turns.pop(session_id, None)
        if task:
            task.cancel()
        state.sessions.pop(session_id, None)
        state.messages.pop(session_id, None)
        state.session_status.pop(session_id, None)
        await state.broadcast(SessionDeletedEvent.create(session_id, session))
        return True

    @app.get("/session/{session_id}/children")
    async def session_children(session_id: str) -> list[Session]:
        return [s for s in state.sessions.values() if s.parent_id == session_id]

    @app.post("/session/{session_id}/abort")
    async def abort_session(session_id: str) -> bool:
        get_session(session_id)
        task = state.running_turns.get(session_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    @app.post("/session/{session_id}/init")
    async def init_session(session_id: str, body: dict[str, Any] | None = None) -> bool:
        get_session(session_id)
        return True

    @app.get("/session/{session_id}/todo")
    async def session_todos(session_id: str) -> list[Todo]:
        return state.todos.get(session_id, [])

    @app.get("/session/{session_id}/diff")
    async def session_diff(session_id: str) -> list[FileDiff]:
        # The agent's changes = the working-tree diff vs HEAD in the project dir (the shim runs on
        # the user's machine, so it can just ask git). messageID-scoped diffs would need snapshots.
        diffs = await local_tools.git_file_diffs(Path(state.working_dir))
        return [FileDiff(**d) for d in diffs]

    @app.get("/session/{session_id}/permissions")
    async def session_permissions(session_id: str) -> list[PermissionAskedProperties]:
        return [
            p.properties
            for p in state.pending_permissions.values()
            if p.properties.session_id == session_id
        ]

    # ---------------------------------------------------------------- messages

    @app.get("/session/{session_id}/message")
    async def list_messages(session_id: str, limit: int | None = Query(default=None)) -> list[AnyMessageWithParts]:
        get_session(session_id)
        messages = state.messages.get(session_id, [])
        return messages[-limit:] if limit else messages

    @app.get("/session/{session_id}/message/{message_id}")
    async def get_message(session_id: str, message_id: str) -> AnyMessageWithParts:
        get_session(session_id)
        for msg in state.messages.get(session_id, []):
            if msg.info.id == message_id:
                return msg
        raise HTTPException(status_code=404, detail="Message not found")

    async def start_turn(session_id: str, request: MessageRequest) -> AgentTurn:
        session = get_session(session_id)
        if (task := state.running_turns.get(session_id)) and not task.done():
            raise HTTPException(status_code=409, detail="Session is busy")

        user_message = MessageWithParts[UserMessage](
            info=UserMessage(
                id=identifier.ascending("message", request.message_id),
                session_id=session_id,
                time=TimeCreated(created=now_ms()),
                agent=request.agent or "build",
                model=request.model or ModelRef(provider_id=PROVIDER_ID, model_id=MODEL_ID),
            )
        )
        for part in request.parts:
            if isinstance(part, TextPartInput):
                created = user_message.add_text_part(part.text)
                await state.broadcast(PartUpdatedEvent.create(created))
            # File/agent/subtask inputs: extend here when needed.
        state.messages.setdefault(session_id, []).append(user_message)
        await state.broadcast(MessageUpdatedEvent.create(user_message.info))

        if session.title == "New session":
            text = "\n".join(p.text for p in [pt for pt in request.parts if isinstance(pt, TextPartInput)])
            if text:
                session.title = text[:80]
        touch(session)
        await state.broadcast(SessionUpdatedEvent.create(session))

        turn = AgentTurn(state, session_id, user_message, provider_id=PROVIDER_ID, model_id=MODEL_ID)
        await turn.begin()
        return turn

    async def run_turn(turn: AgentTurn) -> None:
        error: str | None = None
        try:
            await backend.run_turn(turn)
        except asyncio.CancelledError:
            error = "aborted"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            await turn.stream_text(f"\n[backend error] {exc}")
        finally:
            await asyncio.shield(turn.finish(error=error))
            state.running_turns.pop(turn.session_id, None)

    @app.post("/session/{session_id}/prompt_async", status_code=status.HTTP_204_NO_CONTENT)
    async def prompt_async(session_id: str, request: MessageRequest) -> None:
        turn = await start_turn(session_id, request)
        state.running_turns[session_id] = asyncio.create_task(run_turn(turn))

    @app.post("/session/{session_id}/message")
    async def prompt_sync(session_id: str, request: MessageRequest) -> AnyMessageWithParts:
        turn = await start_turn(session_id, request)
        task = asyncio.create_task(run_turn(turn))
        state.running_turns[session_id] = task
        await task
        return turn.message

    # ------------------------------------------------------------- permissions

    @app.get("/permission")
    async def list_permissions() -> list[PermissionAskedProperties]:
        return [p.properties for p in state.pending_permissions.values()]

    @app.post("/permission/{permission_id}/reply")
    @app.post("/session/{session_id}/permissions/{permission_id}")
    async def reply_permission(
        permission_id: str, body: PermissionReplyRequest, session_id: str | None = None
    ) -> bool:
        pending = state.pending_permissions.get(permission_id)
        if pending is None:
            raise HTTPException(status_code=404, detail="Permission not found")
        if not pending.future.done():
            pending.future.set_result(body.reply)
        await state.broadcast(
            PermissionResolvedEvent.create(
                session_id=pending.properties.session_id, request_id=permission_id, reply=body.reply
            )
        )
        return True

    # ------------------------------------------------------------------- files

    @app.get("/file")
    async def list_files(path: str = Query(default="")) -> list[FileNode]:
        target = resolve_path(path) if path else Path(state.working_dir)
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="Directory not found")
        nodes: list[FileNode] = []
        for entry in target.iterdir():
            if entry.name in SKIP_DIRS:
                continue
            rel = str(entry.relative_to(state.working_dir))
            if entry.is_dir():
                nodes.append(FileNode(name=entry.name, path=rel, type="directory"))
            else:
                nodes.append(FileNode(name=entry.name, path=rel, type="file", size=entry.stat().st_size))
        return sorted(nodes, key=lambda n: (n.type != "directory", n.name.lower()))

    @app.get("/file/content")
    async def read_file(path: str = Query()) -> FileContent:
        target = resolve_path(path)
        try:
            return FileContent(path=path, content=target.read_text("utf-8"))
        except FileNotFoundError as err:
            raise HTTPException(status_code=404, detail="File not found") from err
        except UnicodeDecodeError as err:
            raise HTTPException(status_code=400, detail="Cannot read binary file") from err

    @app.get("/file/status")
    async def file_status() -> list[Any]:
        return []

    def iter_project_files(*, max_files: int = 20_000) -> list[Path]:
        results: list[Path] = []
        root = Path(state.working_dir)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                results.append(Path(dirpath) / name)
                if len(results) >= max_files:
                    return results
        return results

    @app.get("/find")
    async def find_text(pattern: str = Query()) -> list[FindMatch]:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {e}") from e
        matches: list[FindMatch] = []
        for file in iter_project_files():
            try:
                content = file.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(file.relative_to(state.working_dir))
            for line_num, line in enumerate(content.splitlines(), 1):
                for m in regex.finditer(line):
                    matches.append(
                        FindMatch.create(
                            path=rel,
                            lines=line.strip(),
                            line_number=line_num,
                            absolute_offset=m.start(),
                            submatches=[SubmatchInfo.create(m.group(), m.start(), m.end())],
                        )
                    )
                    if len(matches) >= 100:
                        return matches
        return matches

    @app.get("/find/file")
    async def find_files(
        query: str = Query(), dirs: str = Query(default="false"), limit: int | None = Query(default=None)
    ) -> list[str]:
        pattern = query if any(c in query for c in "*?[") else f"*{query}*"
        max_results = min(limit or 100, 200)
        results: list[str] = []
        for file in iter_project_files():
            if fnmatch.fnmatch(file.name, pattern):
                results.append(str(file.relative_to(state.working_dir)))
                if len(results) >= max_results:
                    break
        return sorted(results)

    @app.get("/find/symbol")
    async def find_symbols(query: str = Query()) -> list[Any]:
        return []

    return app
