"""Sessions: the agent workflows a server starts, found again through Temporal visibility.

A session is a top-level workflow of an agent in the server's registry, on that agent's task
queue. The server starts it directly and lists sessions with one visibility query, so nothing in
the harness holds a list of sessions and no count of them is bounded by one workflow's state.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from typing import Any

from temporalio.client import Client, WorkflowExecution, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_kit.agent_protocol import TITLE_MEMO, AgentConfig
from temporal_agent_kit.web.registry import AgentDescriptor, AgentRegistry

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000


@dataclass
class Session:
    """One session, as the session list shows it."""

    workflow_id: str
    agent_workflow_type: str
    # The agent's label from the registry.
    label: str
    created_at: float
    execution_status: str
    closed: bool
    # The agent's title (``TITLE_MEMO``), by default its first message. ``None`` until it has one.
    title: str | None = None


@dataclass
class SessionPage:
    """One page of sessions, newest first."""

    sessions: list[Session]
    # Pass back as ``page_token`` for the next page; ``None`` on the last one.
    next_page_token: str | None


class InvalidPageToken(ValueError):
    """A ``page_token`` that is not one this server handed out."""


def sessions_query(registry: AgentRegistry) -> str:
    """The visibility query matching every session of ``registry``'s agents.

    Each agent is matched on its workflow type and its task queue together, so deployments
    isolated by ``TASK_QUEUE_PREFIX`` in one namespace list only their own sessions.
    ``ParentWorkflowId IS NULL`` leaves out subagents, which are their parent agent's children.
    """
    agents = " OR ".join(
        f"(WorkflowType = {_quoted(agent.workflow_type)} AND TaskQueue = {_quoted(agent.task_queue)})"
        for agent in registry.agents
    )
    return f"({agents}) AND ParentWorkflowId IS NULL"


async def start_session(
    temporal: Client,
    agent: AgentDescriptor,
    registry: AgentRegistry,
    *,
    data: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> Session:
    """Start a session of ``agent``, or return the running one ``session_id`` names.

    Idempotent while the session runs, so a caller that cannot know whether it has been here
    before (a chat integration handling a redelivered webhook) can ask every time. A
    ``session_id`` whose workflow has closed starts a new run under the same id.
    """
    config = AgentConfig()
    handle = await temporal.start_workflow(
        agent.workflow_type,
        args=[config] if data is None else [config, data],
        id=session_id or f"agent-session-{uuid.uuid4()}",
        task_queue=agent.task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    session = await get_session(temporal, registry, handle.id)
    if session is None:
        raise SessionIdTaken(handle.id)
    return session


class SessionIdTaken(ValueError):
    """A ``session_id`` that names a running workflow which is not a session of this server."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"{session_id!r} names a running workflow that is not a session of this server's agents."
        )


async def list_sessions(
    temporal: Client,
    registry: AgentRegistry,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    page_token: str | None = None,
) -> SessionPage:
    """One page of ``registry``'s sessions, newest first, from a single visibility call."""
    if not registry.agents:
        return SessionPage(sessions=[], next_page_token=None)
    executions = temporal.list_workflows(
        sessions_query(registry),
        page_size=min(max(page_size, 1), MAX_PAGE_SIZE),
        next_page_token=_decoded_page_token(page_token),
    )
    try:
        await executions.fetch_next_page()
    except RPCError as exc:
        # The query is ours, so an invalid argument is the token the caller passed.
        if page_token is not None and exc.status == RPCStatusCode.INVALID_ARGUMENT:
            raise InvalidPageToken("page_token is not a page token this server returned.") from exc
        raise
    sessions = [
        await _session(execution, descriptor)
        for execution in executions.current_page or []
        if (descriptor := registry.by_workflow_type(execution.workflow_type)) is not None
    ]
    return SessionPage(
        sessions=sessions,
        next_page_token=_encoded_page_token(executions.next_page_token),
    )


async def get_session(
    temporal: Client, registry: AgentRegistry, session_id: str
) -> Session | None:
    """The session ``session_id`` names, or ``None`` when no session of ``registry`` has it."""
    try:
        execution = await temporal.get_workflow_handle(session_id).describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    descriptor = registry.by_workflow_type(execution.workflow_type)
    if (
        descriptor is None
        or execution.task_queue != descriptor.task_queue
        or execution.parent_id is not None
    ):
        return None
    return await _session(execution, descriptor)


async def _session(execution: WorkflowExecution, agent: AgentDescriptor) -> Session:
    status = execution.status
    return Session(
        workflow_id=execution.id,
        agent_workflow_type=execution.workflow_type,
        label=agent.label,
        created_at=execution.start_time.timestamp(),
        execution_status=status.name if status is not None else "UNSPECIFIED",
        closed=status is not WorkflowExecutionStatus.RUNNING,
        title=await execution.memo_value(TITLE_MEMO, None, type_hint=str),
    )


def _quoted(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _encoded_page_token(token: bytes | None) -> str | None:
    return base64.urlsafe_b64encode(token).decode() if token else None


def _decoded_page_token(token: str | None) -> bytes | None:
    if not token:
        return None
    try:
        return base64.b64decode(token.encode(), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidPageToken("page_token is not a page token this server returned.") from exc
