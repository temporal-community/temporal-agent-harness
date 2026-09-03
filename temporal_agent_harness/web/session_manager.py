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

    def merged_with(self, other: AgentRegistry) -> AgentRegistry:
        """This registry plus ``other``, one entry per workflow type, ``other`` winning ties.

        Keyed on workflow type rather than key because the merged registry exists to identify
        running workflows, and that is the only field a workflow execution carries. ``other``
        wins so a relabelled or re-queued agent takes effect rather than being shadowed by
        whatever a server pushed months ago.
        """
        by_type = {agent.workflow_type: agent for agent in [*self.agents, *other.agents]}
        return AgentRegistry(agents=list(by_type.values()))


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
    is_discovered: bool = False
    """True if this session wasn't started via ``create_session`` but was found already running
    in the namespace (see ``_discover_untracked_sessions`` in ``web/app.py``)."""
    is_archived: bool = False
    """Kept out of the session list by default.

    A flag rather than a removal: the workflow's history outlives this list either way, so a
    deep link into an archived session still resolves and archiving stays undoable. Defaulted
    so a manager running older code, whose query answers without this field at all, decodes as
    "not archived" rather than failing."""


@dataclass
class SetSessionsArchivedRequest:
    """Archive or restore some sessions by workflow id.

    Takes a list because the clutter this exists to clear arrives in bulk. Nothing has ever
    removed an entry from this list, while the namespace's retention keeps deleting the
    workflows underneath it, so what accumulates is dozens of corpses at once — and clearing
    them one update at a time is one workflow task each.
    """

    workflow_ids: list[str]
    is_archived: bool = True


@workflow.defn
class SessionManagerWorkflow:
    """Long-running parent workflow that manages agent sessions."""

    @workflow.init
    def __init__(self, registry: AgentRegistry) -> None:
        self._sessions: list[Session] = []
        self._next_number = 1
        self._registry = registry
        self._discoverable = registry

    @workflow.query
    def available_agents(self) -> AgentRegistry:
        return self._registry

    @workflow.query
    def discoverable_agents(self) -> AgentRegistry:
        """Every agent type this manager has ever been told about.

        Two different questions were being answered by ``available_agents``, and only one of
        them is about the server asking. "What can I create?" genuinely depends on which agents
        the current server serves. "What is already running?" does not: a session's existence
        has nothing to do with whether the server you happen to be talking to could have
        started it. Session discovery reads this one — see ``_discover_untracked_sessions`` in
        ``web/app.py``, which can only recognise an agent workflow by its type, so the set it
        is handed decides which running sessions are visible at all.

        Gating that on the creation registry meant restarting a server that serves seven agents
        withdrew the other servers' types from the query, and live sessions of those types
        dropped out of the console mid-conversation while their workflows kept running.
        """
        return self._discoverable

    @workflow.update
    def set_available_agents(self, registry: AgentRegistry) -> AgentRegistry:
        """Replace the set of agents this manager offers.

        The manager outlives every server that connects to it, so the registry it was
        constructed with is the one from the first server ever to start it — and it answers
        ``available_agents`` for all of them. Without a way to replace it, changing what a
        server serves does nothing anyone can see: the registry edit is real, the console keeps
        listing what it listed yesterday, and the first sign of it is a session opened against
        an agent no worker is running.

        Sessions already under way are untouched. They are addressed by workflow id and
        abandoned by this parent, so withdrawing an agent only stops it being offered; a
        conversation in progress against it continues.

        Only what is OFFERED is replaced. ``discoverable_agents`` accumulates instead, because
        a type dropping off this list says the current server stopped serving it, never that
        its running sessions stopped existing.
        """
        self._registry = registry
        self._discoverable = self._discoverable.merged_with(registry)
        return self._registry

    @workflow.update
    def set_sessions_archived(
        self, request: SetSessionsArchivedRequest
    ) -> list[Session]:
        """Archive or restore sessions, and report back the ones this changed.

        Only the flag moves. Ending a session is the ``close`` signal on the agent itself, and
        the two are deliberately separate here: this workflow should not be the thing that
        decides a conversation is over. The caller that archives a session still running is the
        one that has to close it — see the archive endpoint, which does exactly that, so hiding
        a session can never leave a live agent running where nobody will look for it.

        Unknown ids are ignored rather than raised on. A session can be archived from one
        browser tab while another still lists it, and failing the whole batch because one entry
        has already gone would make the bulk case fail exactly when it is most useful.
        """
        wanted = set(request.workflow_ids)
        changed: list[Session] = []
        for session in self._sessions:
            if (
                session.workflow_id in wanted
                and session.is_archived != request.is_archived
            ):
                session.is_archived = request.is_archived
                changed.append(session)
        return changed

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
            # EXPLICIT: a session outlives its manager. The manager is shared infrastructure
            # every browser session is started through, so the SDK default of TERMINATE would
            # make restarting it — or replacing its registry, or continuing it as new — kill
            # every live agent session, including ones a person is mid-conversation with.
            # Sessions are addressed by workflow id and end through their own `close` signal,
            # so nothing here needs the parent-close relationship.
            #
            # It is also what the live manager's history already records on all 36 of its child
            # starts. The replayer does not compare this attribute, so leaving it at the
            # default replayed clean while quietly meaning that any session created after a
            # cutover would be the one child of this manager that dies with it.
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
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
