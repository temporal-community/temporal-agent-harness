"""Durable account registry for tools, agents, and agent sessions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from hashlib import sha256
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

REGISTRY_WORKFLOW_ID_PREFIX = "account-registry"
REGISTRY_TASK_QUEUE = "mcp-registry"


def account_registry_workflow_id(account_id: str) -> str:
    """Return the stable workflow ID for one account without exposing account PII."""
    if not account_id.strip():
        raise ValueError("account_id is required")
    digest = sha256(account_id.encode()).hexdigest()
    return f"{REGISTRY_WORKFLOW_ID_PREFIX}-{digest}"


@activity.defn
async def fetch_external_tools(name: str, url: str) -> list[dict[str, Any]]:
    """Fetch an external server's tool list. Prefixes each tool `{name}_{tool}`."""
    activity.logger.info("[registry] fetching tools from %s", url)
    activity.heartbeat()

    async with (
        streamable_http_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.list_tools()

    tools = []
    for tool in result.tools:
        prefixed = tool.model_copy(update={"name": f"{name}_{tool.name}"})
        tools.append(prefixed.model_dump())

    activity.logger.info("[registry] fetched %d tool(s) from %s", len(tools), url)
    return tools


@dataclass
class AccountEntries:
    """Account-owned external routes. MCP tool definitions are fetched live."""

    remote_servers: dict[str, str] = field(default_factory=dict)
    nexus_servers: dict[str, NexusMCPServerRegistration] = field(
        default_factory=dict
    )
    subagent_providers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NexusMCPServerRegistration:
    """Read-only discovery metadata for a directly invoked Nexus MCP service."""

    name: str
    endpoint: str
    service: str


@dataclass(frozen=True)
class AgentRegistration:
    """One account-owned agent definition."""

    agent_id: str
    kind: str
    label: str
    description: str
    nexus_endpoint: str | None = None
    nexus_service: str = "A2AService"
    provider_url: str | None = None


@dataclass(frozen=True)
class SessionRecord:
    """A registry session mapped to its provider-specific instance."""

    account_id: str
    session_id: str
    agent_id: str
    provider_session_id: str
    created_at: float
    label: str
    parent_session_id: str | None = None
    subagent_id: str | None = None
    source_session_id: str | None = None
    is_spawned: bool = False
    is_message_queuing_enabled: bool = False
    has_started: bool = False
    current_turn: int = 0
    closed: bool = False
    discovery_offset: int = 0


@dataclass(frozen=True)
class SessionEvent:
    """A UI-compatible event retained for a minimal external HTTP agent."""

    offset: int
    event_type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PendingSessionEvent:
    event_type: str
    data: dict[str, Any]


@dataclass(frozen=True)
class SpawnedAgentObservation:
    """A registered child instance observed through its parent agent."""

    subagent_id: str
    agent_key: str
    provider_session_id: str
    next_expected_turn: int = 1


@dataclass(frozen=True)
class SubagentInstanceRoute:
    """Route for one instance created by a registered factory."""

    alias: str
    url: str
    provider_instance_id: str


@workflow.defn(sandboxed=False, name="ToolRegistry")
class ToolRegistryWorkflow:
    """One durable control-plane aggregate for a single account."""

    @workflow.init
    def __init__(self, account_id: str) -> None:
        self._account_id = account_id
        self._remote_entries: dict[str, str] = {}
        self._nexus_mcp_entries: dict[str, NexusMCPServerRegistration] = {}
        self._subagent_entries: dict[str, str] = {}
        self._subagent_instances: dict[str, SubagentInstanceRoute] = {}
        self._agents: dict[str, AgentRegistration] = {}
        self._sessions: dict[str, SessionRecord] = {}
        self._session_events: dict[str, list[SessionEvent]] = {}
        self._next_session_number = 1

    @workflow.run
    async def run(self, account_id: str) -> None:
        if account_id != self._account_id:
            raise ApplicationError(
                "workflow account does not match initialized account",
                type="AccountMismatch",
                non_retryable=True,
            )
        await workflow.wait_condition(lambda: False)

    # -- registration ------------------------------------------------------------

    @workflow.signal
    async def register_external(self, name: str, url: str) -> None:
        """Register an MCP server and check that it is reachable."""
        self._remote_entries[name] = url
        try:
            tools = await workflow.execute_activity(
                fetch_external_tools,
                args=[name, url],
                schedule_to_close_timeout=timedelta(seconds=60),
                start_to_close_timeout=timedelta(seconds=45),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except Exception as exc:  # noqa: BLE001 - preserve registration on probe failure
            workflow.logger.warning(
                "[registry] registered %r at %s, validation fetch failed: %s",
                name,
                url,
                exc,
            )
            return
        workflow.logger.info(
            "[registry] registered %r at %s (%d tools)", name, url, len(tools)
        )

    @workflow.signal
    def deregister(self, name: str) -> None:
        """Remove one account-owned MCP registration."""
        if self._remote_entries.pop(name, None) is not None:
            workflow.logger.info("[registry] deregistered %r", name)

    @workflow.signal
    def clear_all(self) -> None:
        """Remove all MCP server and subagent registrations."""
        self._remote_entries.clear()
        self._nexus_mcp_entries.clear()
        self._subagent_entries.clear()
        self._subagent_instances.clear()
        self._agents.clear()
        self._sessions.clear()
        self._session_events.clear()
        workflow.logger.info("[registry] cleared all entries")

    @workflow.signal
    def register_subagent(self, alias: str, url: str) -> None:
        """Register a minimal HTTP subagent provider for this account."""
        self._subagent_entries[alias] = url
        workflow.logger.info("[registry] registered subagent %r at %s", alias, url)

    @workflow.signal
    def deregister_subagent(self, alias: str) -> None:
        """Remove one account-owned subagent provider."""
        if self._subagent_entries.pop(alias, None) is not None:
            workflow.logger.info("[registry] deregistered subagent %r", alias)

    @workflow.update
    def register_nexus_mcp_server(
        self, registration: NexusMCPServerRegistration
    ) -> NexusMCPServerRegistration:
        """Register metadata without changing the service's direct Nexus route."""
        if not all(
            value.strip()
            for value in (
                registration.name,
                registration.endpoint,
                registration.service,
            )
        ):
            raise ApplicationError(
                "name, endpoint, and service are required",
                type="InvalidNexusMCPRegistration",
                non_retryable=True,
            )
        self._nexus_mcp_entries[registration.name] = registration
        return registration

    @workflow.update
    def register_agent(self, registration: AgentRegistration) -> AgentRegistration:
        if registration.kind not in {"harness_nexus", "external_http"}:
            raise ApplicationError(
                f"unsupported agent kind {registration.kind!r}",
                type="UnsupportedAgentKind",
                non_retryable=True,
            )
        if registration.kind == "harness_nexus" and not registration.nexus_endpoint:
            raise ApplicationError(
                "harness_nexus agents require nexus_endpoint",
                type="InvalidAgentRegistration",
                non_retryable=True,
            )
        if registration.kind == "external_http" and not registration.provider_url:
            raise ApplicationError(
                "external_http agents require provider_url",
                type="InvalidAgentRegistration",
                non_retryable=True,
            )
        self._agents[registration.agent_id] = registration
        return registration

    @workflow.update
    def deregister_agent(self, agent_id: str) -> None:
        if any(session.agent_id == agent_id for session in self._sessions.values()):
            raise ApplicationError(
                f"agent {agent_id!r} still has registered sessions",
                type="AgentHasSessions",
                non_retryable=True,
            )
        self._agents.pop(agent_id, None)

    @workflow.update
    def create_session(
        self,
        agent_id: str,
        provider_session_id: str | None = None,
        is_message_queuing_enabled: bool = False,
    ) -> SessionRecord:
        if agent_id not in self._agents:
            raise ApplicationError(
                f"unknown agent {agent_id!r}",
                type="UnknownAgent",
                non_retryable=True,
            )
        session_id = f"session-{workflow.uuid4()}"
        session = SessionRecord(
            account_id=self._account_id,
            session_id=session_id,
            agent_id=agent_id,
            provider_session_id=provider_session_id or session_id,
            created_at=workflow.time(),
            label=f"Session {self._next_session_number}",
            is_message_queuing_enabled=is_message_queuing_enabled,
        )
        self._next_session_number += 1
        self._sessions[session_id] = session
        self._session_events[session_id] = []
        return session

    def _spawned_session(
        self, parent_session_id: str, source_session_id: str
    ) -> SessionRecord | None:
        return next(
            (
                session
                for session in self._sessions.values()
                if session.is_spawned
                and session.parent_session_id == parent_session_id
                and session.source_session_id == source_session_id
            ),
            None,
        )

    def _observe_spawned_agent(
        self,
        parent_session_id: str,
        observation: SpawnedAgentObservation,
    ) -> SessionRecord | None:
        parent = self._require_session(parent_session_id)
        registration = self._agents.get(observation.agent_key)
        if registration is None:
            workflow.logger.info(
                "[registry] ignoring unregistered spawned agent %r from session %r",
                observation.agent_key,
                parent_session_id,
            )
            return None

        provider_session_id = observation.provider_session_id
        if registration.kind == "external_http":
            route = self._subagent_instances.get(observation.provider_session_id)
            if route is None or route.alias != observation.agent_key:
                workflow.logger.info(
                    "[registry] ignoring unresolvable external spawned agent %r (%s)",
                    observation.agent_key,
                    observation.provider_session_id,
                )
                return None
            provider_session_id = route.provider_instance_id

        current_turn = max(0, observation.next_expected_turn - 1)
        existing = self._spawned_session(
            parent_session_id, observation.provider_session_id
        )
        if existing is not None:
            updated = replace(
                existing,
                current_turn=max(existing.current_turn, current_turn),
                closed=False,
            )
            self._sessions[existing.session_id] = updated
            return updated

        session_id = f"session-{workflow.uuid4()}"
        session = SessionRecord(
            account_id=self._account_id,
            session_id=session_id,
            agent_id=observation.agent_key,
            provider_session_id=provider_session_id,
            created_at=workflow.time(),
            label=f"{registration.label} · {observation.subagent_id}",
            parent_session_id=parent.session_id,
            subagent_id=observation.subagent_id,
            source_session_id=observation.provider_session_id,
            is_spawned=True,
            is_message_queuing_enabled=parent.is_message_queuing_enabled,
            has_started=True,
            current_turn=current_turn,
        )
        self._sessions[session_id] = session
        self._session_events[session_id] = []
        workflow.logger.info(
            "[registry] observed spawned agent %r (%s) under session %r",
            observation.agent_key,
            observation.provider_session_id,
            parent_session_id,
        )
        return session

    @workflow.signal
    def record_spawned_agent_batch(
        self,
        parent_session_id: str,
        observations: list[SpawnedAgentObservation],
        stopped_source_session_ids: list[str],
        next_offset: int,
    ) -> None:
        parent = self._require_session(parent_session_id)
        for observation in observations:
            self._observe_spawned_agent(parent_session_id, observation)
        for source_session_id in stopped_source_session_ids:
            session = self._spawned_session(parent_session_id, source_session_id)
            if session is not None:
                self._sessions[session.session_id] = replace(session, closed=True)
        self._sessions[parent_session_id] = replace(
            parent,
            discovery_offset=max(parent.discovery_offset, next_offset),
        )

    @workflow.update
    def sync_spawned_agents(
        self,
        parent_session_id: str,
        observations: list[SpawnedAgentObservation],
    ) -> list[SessionRecord]:
        """Persist active registered children found through the parent's status."""
        self._require_session(parent_session_id)
        synced: list[SessionRecord] = []
        active_source_ids = {
            observation.provider_session_id for observation in observations
        }
        for observation in observations:
            session = self._observe_spawned_agent(parent_session_id, observation)
            if session is not None:
                synced.append(session)
        for session_id, session in tuple(self._sessions.items()):
            if (
                session.parent_session_id == parent_session_id
                and session.is_spawned
                and session.source_session_id not in active_source_ids
                and not session.closed
            ):
                self._sessions[session_id] = replace(session, closed=True)
        return synced

    @workflow.update
    def reconcile_spawned_agents(
        self,
        parent_session_id: str,
        observations: list[SpawnedAgentObservation],
        stopped_source_session_ids: list[str],
        next_offset: int,
        active: list[SpawnedAgentObservation],
    ) -> list[SessionRecord]:
        """Project missed lifecycle events, then reconcile the active snapshot."""
        self.record_spawned_agent_batch(
            parent_session_id,
            observations,
            stopped_source_session_ids,
            next_offset,
        )
        return self.sync_spawned_agents(parent_session_id, active)

    @workflow.update
    def remove_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._session_events.pop(session_id, None)

    @workflow.update
    def mark_session_started(self, session_id: str, current_turn: int) -> SessionRecord:
        session = self._require_session(session_id)
        updated = replace(
            session,
            has_started=True,
            current_turn=max(session.current_turn, current_turn),
        )
        self._sessions[session_id] = updated
        return updated

    @workflow.update
    def append_session_events(
        self, session_id: str, events: list[PendingSessionEvent]
    ) -> int:
        self._require_session(session_id)
        retained = self._session_events.setdefault(session_id, [])
        for event in events:
            retained.append(
                SessionEvent(
                    offset=len(retained) + 1,
                    event_type=event.event_type,
                    data=event.data,
                )
            )
        return len(retained)

    @workflow.update
    def close_session(self, session_id: str) -> SessionRecord:
        session = self._require_session(session_id)
        closed = replace(session, closed=True)
        self._sessions[session_id] = closed
        for child_id, child in tuple(self._sessions.items()):
            if child.parent_session_id == session_id and not child.closed:
                self._sessions[child_id] = replace(child, closed=True)
        return closed

    def _require_session(self, session_id: str) -> SessionRecord:
        session = self._sessions.get(session_id)
        if session is None:
            raise ApplicationError(
                f"unknown session {session_id!r}",
                type="UnknownSession",
                non_retryable=True,
            )
        return session

    @workflow.update
    def bind_subagent_instance(
        self,
        instance_id: str,
        route: SubagentInstanceRoute,
    ) -> None:
        """Bind a gateway task to one provider route.

        A2A starts third-party tasks lazily: the first binding knows the alias and
        URL, while the provider task ID arrives with the first response. Permit
        that one-way promotion, and make a retried provisional bind a no-op once
        the provider ID is known. Neither case may change the selected provider.
        """
        existing = self._subagent_instances.get(instance_id)
        if existing is not None:
            same_provider = existing.alias == route.alias and existing.url == route.url
            provider_id_is_compatible = (
                existing.provider_instance_id == route.provider_instance_id
                or not existing.provider_instance_id
                or not route.provider_instance_id
            )
            if not same_provider or not provider_id_is_compatible:
                raise ApplicationError(
                    f"subagent instance {instance_id!r} has a different route",
                    type="SubagentInstanceConflict",
                    non_retryable=True,
                )
            if existing.provider_instance_id and not route.provider_instance_id:
                return
        self._subagent_instances[instance_id] = route

    @workflow.update
    def unbind_subagent_instance(self, instance_id: str) -> None:
        """Remove one gateway instance route."""
        self._subagent_instances.pop(instance_id, None)

    # -- queries -----------------------------------------------------------------

    @workflow.query
    def find(self, name: str) -> str | None:
        return self._remote_entries.get(name)

    @workflow.query
    def find_subagent(self, alias: str) -> str | None:
        return self._subagent_entries.get(alias)

    @workflow.query
    def find_subagent_instance(self, instance_id: str) -> SubagentInstanceRoute | None:
        """Return the provider route for one gateway instance."""
        return self._subagent_instances.get(instance_id)

    @workflow.query
    def list_account_entries(self) -> AccountEntries:
        return AccountEntries(
            remote_servers=dict(self._remote_entries),
            nexus_servers=dict(self._nexus_mcp_entries),
            subagent_providers=dict(self._subagent_entries),
        )

    @workflow.query
    def list_agents(self) -> list[AgentRegistration]:
        return list(self._agents.values())

    @workflow.query
    def get_agent(self, agent_id: str) -> AgentRegistration | None:
        return self._agents.get(agent_id)

    @workflow.query
    def list_sessions(self) -> list[SessionRecord]:
        return list(self._sessions.values())

    @workflow.query
    def get_session(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)

    @workflow.query
    def resolve_session(self, session_id: str) -> SessionRecord | None:
        """Resolve an account session by its public ID or registered provider alias."""
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        return next(
            (
                candidate
                for candidate in self._sessions.values()
                if candidate.source_session_id == session_id
                or candidate.provider_session_id == session_id
            ),
            None,
        )

    @workflow.query
    def poll_session_events(
        self, session_id: str, from_offset: int
    ) -> list[SessionEvent]:
        return [
            event
            for event in self._session_events.get(session_id, [])
            if event.offset > from_offset
        ]
