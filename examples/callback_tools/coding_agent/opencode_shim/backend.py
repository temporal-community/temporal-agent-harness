"""The backend seam.

Implement `AgentBackend.run_turn` against your Temporal harness:
start/signal the workflow with the prompt, then translate your durable
event stream into calls on the `AgentTurn` helper. The shim takes care of
OpenCode wire formats, event broadcasting, and message bookkeeping.

This example's implementation is `HarnessBackend` (see `harness_backend.py`),
which fronts the `CodingAgent` workflow through the packaged harness HTTP server.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, Protocol

from . import _identifiers as identifier
from ._time import now_ms
from .models import (
    AssistantMessage,
    MessagePath,
    MessageTime,
    MessageUpdatedEvent,
    MessageWithParts,
    PartUpdatedEvent,
    PermissionAskedProperties,
    PermissionRequestEvent,
    PermissionToolInfo,
    ReasoningPart,
    SessionIdleEvent,
    SessionStatus,
    SessionStatusEvent,
    TextPart,
    TimeStart,
    TimeStartEnd,
    TimeStartEndCompacted,
    TimeStartEndOptional,
    Todo,
    ToolPart,
    ToolStateCompleted,
    ToolStateError,
    ToolStateRunning,
    UserMessage,
)
from .state import PendingPermission, ShimState

PermissionReply = Literal["once", "always", "reject"]


class ToolHandle:
    """Handle for a single tool invocation rendered in the TUI.

    Use OpenCode's canonical tool names + camelCase args to get rich
    rendering: read/list/glob/grep/webfetch/task/bash/edit/write/todowrite
    (e.g. bash -> {"command": ...}, edit -> {"filePath", "oldString",
    "newString"}). Anything else renders generically.
    """

    def __init__(self, turn: AgentTurn, part: ToolPart, started: int) -> None:
        self._turn = turn
        self.part = part
        self._started = started

    async def complete(
        self,
        output: str,
        *,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.part.state = ToolStateCompleted(
            input=self._input(),
            output=output,
            title=title,
            metadata=metadata or {},
            time=TimeStartEndCompacted(start=self._started, end=now_ms()),
        )
        await self._turn._emit_part(self.part)

    async def error(self, message: str) -> None:
        self.part.state = ToolStateError(
            input=self._input(),
            error=message,
            time=TimeStartEnd(start=self._started, end=now_ms()),
        )
        await self._turn._emit_part(self.part)

    def _input(self) -> dict[str, Any]:
        state = self.part.state
        return state.input if hasattr(state, "input") else {}


class AgentTurn:
    """One assistant turn: owns the assistant message and event emission."""

    def __init__(
        self,
        state: ShimState,
        session_id: str,
        user_message: MessageWithParts[UserMessage],
        *,
        provider_id: str = "temporal",
        model_id: str = "agent",
    ) -> None:
        self._state = state
        self.session_id = session_id
        self.user_message = user_message
        info = AssistantMessage(
            id=identifier.ascending("message"),
            session_id=session_id,
            parent_id=user_message.info.id,
            model_id=model_id,
            provider_id=provider_id,
            path=MessagePath(cwd=state.working_dir, root=state.working_dir),
            time=MessageTime(created=now_ms()),
        )
        self.message: MessageWithParts[AssistantMessage] = MessageWithParts(info=info)
        self._text_part: TextPart | None = None
        self._reasoning_part: ReasoningPart | None = None

    @property
    def prompt_text(self) -> str:
        """Concatenated text of the user message parts."""
        return "\n".join(
            p.text for p in self.user_message.parts if isinstance(p, TextPart)
        )

    async def begin(self) -> None:
        """Register the assistant message and mark the session busy."""
        self._state.messages.setdefault(self.session_id, []).append(self.message)
        await self._state.broadcast(MessageUpdatedEvent.create(self.message.info))
        busy = SessionStatus(type="busy")
        self._state.session_status[self.session_id] = busy
        await self._state.broadcast(SessionStatusEvent.create(self.session_id, busy))

    async def stream_text(self, delta: str) -> None:
        """Append streaming text; creates the text part on first call."""
        self.end_reasoning()  # reply text ends the current thinking block
        if self._text_part is None:
            self._text_part = TextPart(
                id=identifier.ascending("part"),
                message_id=self.message.info.id,
                session_id=self.session_id,
                text="",
            )
            self.message.parts.append(self._text_part)
        self._text_part.text += delta
        await self._state.broadcast(PartUpdatedEvent.create(self._text_part, delta=delta))

    def end_text(self) -> None:
        """Close the current text part; the next stream_text starts a new one."""
        self._text_part = None

    async def stream_reasoning(self, delta: str) -> None:
        """Append a streamed thought-summary chunk; creates the reasoning part on first call.

        Renders as OpenCode's collapsible "thinking" block (a ``reasoning`` message part)."""
        if self._reasoning_part is None:
            self._reasoning_part = ReasoningPart(
                id=identifier.ascending("part"),
                message_id=self.message.info.id,
                session_id=self.session_id,
                text="",
                time=TimeStartEndOptional(start=now_ms()),
            )
            self.message.parts.append(self._reasoning_part)
        self._reasoning_part.text += delta
        await self._state.broadcast(
            PartUpdatedEvent.create(self._reasoning_part, delta=delta)
        )

    def end_reasoning(self) -> None:
        """Close the current reasoning part (stamps its end time); the next thought starts a new one."""
        if self._reasoning_part is not None:
            self._reasoning_part.time.end = now_ms()
            self._reasoning_part = None

    def set_todos(self, todos: list[dict[str, Any]]) -> None:
        """Record the agent's task list so ``GET /session/{id}/todo`` reflects it (the live todo
        panel). The tool card itself renders from the tool part's todos; this backs the panel."""
        self._state.todos[self.session_id] = [
            Todo(content=str(t.get("content", "")), status=str(t.get("status", "pending")))
            for t in todos
        ]

    async def tool_start(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        title: str | None = None,
    ) -> ToolHandle:
        """Emit a tool part in the running state."""
        self.end_text()
        self.end_reasoning()
        started = now_ms()
        part = ToolPart(
            id=identifier.ascending("part"),
            message_id=self.message.info.id,
            session_id=self.session_id,
            call_id=call_id or identifier.ascending("call"),
            tool=tool,
            state=ToolStateRunning(time=TimeStart(start=started), input=args, title=title),
        )
        self.message.parts.append(part)
        await self._state.broadcast(PartUpdatedEvent.create(part))
        return ToolHandle(self, part, started)

    async def request_permission(
        self,
        tool: str,
        *,
        patterns: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> PermissionReply:
        """Ask the TUI for permission and wait for the user's reply."""
        permission_id = identifier.ascending("permission")
        props = PermissionAskedProperties(
            id=permission_id,
            session_id=self.session_id,
            permission=tool,
            # `patterns` and `metadata` are what the TUI's permission dialog renders. The TUI's
            # per-tool renderer keys on the tool-specific metadata (e.g. bash -> metadata.command,
            # edit -> metadata.filePath), so the caller passes the OpenCode-shaped args as
            # `metadata`; `patterns` is a readable one-line preview used for allow-listing.
            patterns=patterns or [],
            metadata=metadata or {},
            always=patterns or [tool],
            tool=PermissionToolInfo(message_id=self.message.info.id, call_id=call_id),
        )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._state.pending_permissions[permission_id] = PendingPermission(props, future)
        # Broadcast the SAME props we stored — the live `permission.asked` event must carry the
        # real metadata/patterns, not a lossy reconstruction, or the TUI shows an empty body.
        await self._state.broadcast(PermissionRequestEvent(properties=props))
        try:
            return await future  # type: ignore[return-value]
        finally:
            self._state.pending_permissions.pop(permission_id, None)

    async def finish(self, *, error: str | None = None) -> None:
        """Complete the assistant message and mark the session idle."""
        self.message.info.time.completed = now_ms()
        self.message.info.finish = "error" if error else "stop"
        await self._state.broadcast(MessageUpdatedEvent.create(self.message.info))
        idle = SessionStatus(type="idle")
        self._state.session_status[self.session_id] = idle
        await self._state.broadcast(SessionStatusEvent.create(self.session_id, idle))
        await self._state.broadcast(SessionIdleEvent.create(self.session_id))

    async def _emit_part(self, part: Any) -> None:
        await self._state.broadcast(PartUpdatedEvent.create(part))


class AgentBackend(Protocol):
    """Implement this against your Temporal harness."""

    async def run_turn(self, turn: AgentTurn) -> None:
        """Handle one user prompt.

        Called with `begin()` already done. Stream output via
        `turn.stream_text`, `turn.tool_start`, `turn.request_permission`.
        The server calls `turn.finish()` when this returns (or raises),
        and cancels the task on POST /session/{id}/abort.
        """
        ...
