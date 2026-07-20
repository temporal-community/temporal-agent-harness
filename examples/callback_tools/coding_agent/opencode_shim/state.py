"""In-memory server state and event bus for the OpenCode shim."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .models import (
    AnyMessageWithParts,
    PermissionAskedProperties,
    Session,
    SessionStatus,
    Todo,
)

if TYPE_CHECKING:
    from .models.base import OpenCodeBaseModel


@dataclass
class PendingPermission:
    """A permission request awaiting a reply from the TUI."""

    properties: PermissionAskedProperties
    future: asyncio.Future[str]  # resolves to "once" | "always" | "reject"


@dataclass
class ShimState:
    """Shared in-memory state.

    Everything here is ephemeral by design: the durable source of truth is
    your Temporal workflow. On restart, `AgentBackend.list_sessions` /
    `load_messages` (if you implement them) can repopulate from workflow
    queries or your own store.
    """

    working_dir: str
    project_id: str = "proj_default"
    start_time: float = field(default_factory=time.time)

    sessions: dict[str, Session] = field(default_factory=dict)
    session_status: dict[str, SessionStatus] = field(default_factory=dict)
    messages: dict[str, list[AnyMessageWithParts]] = field(default_factory=dict)
    todos: dict[str, list[Todo]] = field(default_factory=dict)

    running_turns: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    pending_permissions: dict[str, PendingPermission] = field(default_factory=dict)

    event_subscribers: list[asyncio.Queue[Any]] = field(default_factory=list)

    async def broadcast(self, event: OpenCodeBaseModel) -> None:
        """Broadcast an event model to all SSE subscribers."""
        for queue in self.event_subscribers:
            await queue.put(event)

    def status(self, session_id: str) -> SessionStatus:
        return self.session_status.setdefault(session_id, SessionStatus(type="idle"))
