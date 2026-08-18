"""Agent-agnostic session-manager workflow for the harness web API.

The manager owns browser-visible agent sessions: ``create_session`` starts the selected agent as
the manager's own child workflow on that agent's configured task queue, while ``track_session``
lists one a caller already started elsewhere. It intentionally knows only the standard harness
protocol, not any concrete example agent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_protocol import AgentConfig

SESSION_MANAGER_ID = "session-manager"
SESSION_MANAGER_TASK_QUEUE = "session-manager"


@dataclass
class AgentDescriptor:
    """One launchable agent in the web session registry."""

    key: str
    workflow_type: str
    task_queue: str
    label: str
    description: str


@dataclass
class AgentRegistry:
    """The launchable agents known to one session manager."""

    agents: list[AgentDescriptor] = field(default_factory=list)

    def by_workflow_type(self, workflow_type: str) -> AgentDescriptor | None:
        return next(
            (agent for agent in self.agents if agent.workflow_type == workflow_type),
            None,
        )

    def by_key(self, key: str) -> AgentDescriptor | None:
        return next((agent for agent in self.agents if agent.key == key), None)


@dataclass
class CreateSessionRequest:
    """Request to launch one child agent workflow."""

    agent_workflow_type: str
    config: AgentConfig
    task_queue: str | None = None


@dataclass
class TrackSessionRequest:
    """Request to list an already-running agent workflow as a session, labeled like one
    ``create_session`` started. A caller starts its own workflow when it needs its own workflow-ID
    dedupe scheme or its own run/task timeouts, neither of which ``create_session`` exposes.

    * ``agent_workflow_type`` must name a registered agent on every call, including a repeat one,
      or the call fails non-retryably. Only registry membership is checked; nothing compares it to
      the tracked workflow's actual type, so a registered but wrong type mislabels the session.
    * Nothing here verifies ``workflow_id`` exists or is running; that is the caller's burden.
    * ``is_message_queuing_enabled`` is caller-asserted, not derived from the real running agent.
    * ``created_at`` is the tracked workflow's start time in Unix epoch seconds as the caller
      reports it, defaulting to the time of this call. The UI sorts sessions by it and shows it as
      the run's start time. A non-finite value fails non-retryably on every call, including a
      repeat one, since it would break the whole sessions listing, not just this session's row.
    * A repeat call for an already-tracked ``workflow_id`` returns the existing session unchanged.
    """

    workflow_id: str
    agent_workflow_type: str
    is_message_queuing_enabled: bool = False
    created_at: float | None = None


@dataclass
class Session:
    """A single agent session tracked by the manager."""

    workflow_id: str
    created_at: float
    label: str
    agent_workflow_type: str
    is_message_queuing_enabled: bool = False


@workflow.defn
class SessionManagerWorkflow:
    """Long-running workflow that manages agent sessions.

    The session list is only this workflow execution's state: terminate the manager and start it
    again and the list is empty, so a tracked workflow that is not re-tracked stays invisible to
    the UI even though it is still running.
    """

    @workflow.init
    def __init__(self, registry: AgentRegistry) -> None:
        self._sessions: list[Session] = []
        self._next_number = 1
        self._registry = registry

    @workflow.query
    def available_agents(self) -> AgentRegistry:
        return self._registry

    @workflow.update
    async def create_session(self, request: CreateSessionRequest) -> Session:
        descriptor = self._resolve_descriptor(request.agent_workflow_type)

        task_queue = request.task_queue or descriptor.task_queue
        session_id = f"agent-session-{workflow.uuid4()}"
        await workflow.start_child_workflow(
            request.agent_workflow_type,
            request.config,
            id=session_id,
            task_queue=task_queue,
        )

        session = Session(
            workflow_id=session_id,
            created_at=workflow.time(),
            label=f"Session {self._next_number}",
            is_message_queuing_enabled=bool(request.config.is_message_queuing_enabled),
            agent_workflow_type=request.agent_workflow_type,
        )
        self._next_number += 1
        self._sessions.append(session)
        return session

    @workflow.update
    def track_session(self, request: TrackSessionRequest) -> Session:
        self._resolve_descriptor(request.agent_workflow_type)
        if request.created_at is not None and not math.isfinite(request.created_at):
            raise ApplicationError(
                f"created_at must be a finite number, got {request.created_at!r}.",
                type="InvalidCreatedAt",
                non_retryable=True,
            )

        existing = next(
            (s for s in self._sessions if s.workflow_id == request.workflow_id), None
        )
        if existing is not None:
            return existing

        session = Session(
            workflow_id=request.workflow_id,
            created_at=(
                workflow.time() if request.created_at is None else request.created_at
            ),
            label=f"Session {self._next_number}",
            is_message_queuing_enabled=request.is_message_queuing_enabled,
            agent_workflow_type=request.agent_workflow_type,
        )
        self._next_number += 1
        self._sessions.append(session)
        return session

    @workflow.query
    def list_sessions(self) -> list[Session]:
        return list(self._sessions)

    def _resolve_descriptor(self, agent_workflow_type: str) -> AgentDescriptor:
        descriptor = self._registry.by_workflow_type(agent_workflow_type)
        if descriptor is None:
            known = [agent.workflow_type for agent in self._registry.agents]
            raise ApplicationError(
                f"Unknown agent type {agent_workflow_type!r}. "
                f"Known agents: {known}",
                type="UnknownAgentType",
                non_retryable=True,
            )
        return descriptor

    @workflow.run
    async def run(self, registry: AgentRegistry) -> None:
        await workflow.wait_condition(lambda: False)
