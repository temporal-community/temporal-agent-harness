# ABOUTME: Session manager workflow — starts and tracks agent sessions as child
# workflows. Owns the SESSION_MANAGER_ID singleton; the FastAPI server reconnects to it
# by ID on startup.
#
# The manager is AGENT-AGNOSTIC: it knows nothing about any specific agent. Every harness
# agent shares one contract — its @workflow.run takes a single AgentConfig and it talks
# over the standard user_input/agent_status protocol — so the manager can launch ANY of
# them purely by registered workflow-type name. A CreateSessionRequest names the agent
# type (and optionally the task queue it runs on); the manager starts that child and
# tracks it. One manager can therefore own MontyChatAgent, MontyDynamicAgent, and any
# future agent at once.

from dataclasses import dataclass, field

from temporalio import workflow
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness.agent_protocol import AgentConfig

SESSION_MANAGER_ID = "session-manager"

# The task queue the SessionManagerWorkflow itself runs on, hosted by the session-manager worker
# (examples/session_manager/worker.py). Distinct from the per-agent queues in the registry: the
# manager dispatches each child agent session to that agent's own queue (from agents.toml),
# so this queue carries only the manager workflow, never an agent.
SESSION_MANAGER_TASK_QUEUE = "session-manager"


@dataclass
class AgentDescriptor:
    """One launchable agent in the registry.

    Mirrors a single entry of ``examples/session_manager/agents.toml``. The manager receives a list of
    these as its init arg (loaded from that TOML by whatever process starts it — see
    :func:`examples.session_manager.agent_registry.load_agent_registry`) and serves them back over the
    :meth:`SessionManagerWorkflow.available_agents` query, so callers discover the agent
    roster without hardcoding it.

    ``key`` is a short, stable id callers use to pick an agent. ``workflow_type`` is the
    agent's registered ``@workflow.defn`` name (all the manager needs to launch it).
    ``task_queue`` is the queue that agent's worker polls — the manager dispatches the
    session there, so callers never supply a queue. ``label``/``description`` are for
    human + LLM-facing surfaces.
    """

    key: str
    workflow_type: str
    task_queue: str
    label: str
    description: str


@dataclass
class AgentRegistry:
    """The set of launchable agents — what EXISTS, nothing more.

    Passed to the manager at startup and returned verbatim by ``available_agents``.
    Deliberately holds no notion of a "default" agent: which agent to default to is a
    caller's policy decision (the CLI, the web UI), not the registry's.
    """

    agents: list[AgentDescriptor] = field(default_factory=list)

    def by_workflow_type(self, workflow_type: str) -> "AgentDescriptor | None":
        return next(
            (a for a in self.agents if a.workflow_type == workflow_type), None
        )

    def by_key(self, key: str) -> "AgentDescriptor | None":
        return next((a for a in self.agents if a.key == key), None)


@dataclass
class CreateSessionRequest:
    """What to launch for a new session.

    ``agent_workflow_type`` is the agent's registered ``@workflow.defn`` name (e.g.
    ``"MontyChatAgent"`` or ``"MontyDynamicAgent"``) — the only thing the manager needs to pick
    which agent to run, since they all share the standardized AgentConfig input. It must
    name an agent in the manager's registry, or the update is rejected.

    ``config`` is that standardized input, forwarded verbatim to the agent.

    ``task_queue`` is the queue the agent's worker polls. Optional: when omitted the
    manager derives it from the registry entry for ``agent_workflow_type`` (the registry
    is the source of truth for where each agent runs). An explicit value still wins, for
    callers that need to override it.
    """

    agent_workflow_type: str
    config: AgentConfig
    task_queue: str | None = None


@dataclass
class Session:
    """A single agent session tracked by the manager."""

    workflow_id: str
    created_at: float  # epoch seconds
    label: str
    agent_workflow_type: str  # which agent type backs this session
    is_message_queuing_enabled: bool = False


@workflow.defn
class SessionManagerWorkflow:
    """Long-running parent workflow that manages agent session lifecycle.

    Each session is a child agent workflow, started by type name (see
    :class:`CreateSessionRequest`). The default parent close policy is TERMINATE, so all
    child sessions are killed when the manager terminates.

    Uses a deterministic workflow ID (``SESSION_MANAGER_ID``) so the server can reconnect
    after restart.
    """

    @workflow.init
    def __init__(self, registry: AgentRegistry) -> None:
        self._sessions: list[Session] = []
        self._next_number: int = 1
        # The agent roster, supplied at startup by whatever process started the manager
        # (it read examples/session_manager/agents.toml). Source of truth for which agents may be launched
        # and where each runs; served back over `available_agents` for discovery.
        self._registry: AgentRegistry = registry

    @workflow.query
    def available_agents(self) -> AgentRegistry:
        """The registry of launchable agents — the discovery endpoint callers read to
        learn which agents exist without hardcoding them. Callers decide their own
        default; the registry only says what's available."""
        return self._registry

    @workflow.update
    async def create_session(self, request: CreateSessionRequest) -> Session:
        """Start a new agent session as a child workflow of the requested type.

        The requested ``agent_workflow_type`` must be in the registry; its task queue is
        taken from the registry entry unless the request overrides it explicitly.
        """
        descriptor = self._registry.by_workflow_type(request.agent_workflow_type)
        if descriptor is None:
            known = [a.workflow_type for a in self._registry.agents]
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
        # ``registry`` is consumed by @workflow.init (which shares this signature); run()
        # itself just parks forever — sessions are managed via updates/queries.
        await workflow.wait_condition(lambda: False)
