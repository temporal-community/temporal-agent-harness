"""Agent-agnostic session-manager workflow for the harness web API.

The manager owns browser-visible agent sessions and starts each selected agent
as a child workflow on that agent's configured task queue. It intentionally
knows only the standard harness protocol, not any concrete example agent.
"""

from __future__ import annotations

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
class Session:
    """A single child agent session tracked by the manager."""

    workflow_id: str
    created_at: float
    label: str
    agent_workflow_type: str
    is_message_queuing_enabled: bool = False


@workflow.defn
class SessionManagerWorkflow:
    """Long-running parent workflow that manages agent sessions."""

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
        descriptor = self._registry.by_workflow_type(request.agent_workflow_type)
        if descriptor is None:
            known = [agent.workflow_type for agent in self._registry.agents]
            raise ApplicationError(
                f"Unknown agent type {request.agent_workflow_type!r}. "
                f"Known agents: {known}",
                type="UnknownAgentType",
                non_retryable=True,
            )

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

    @workflow.query
    def list_sessions(self) -> list[Session]:
        return list(self._sessions)

    @workflow.run
    async def run(self, registry: AgentRegistry) -> None:
        await workflow.wait_condition(lambda: False)
