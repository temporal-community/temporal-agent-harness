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

from .resources import (
    AccountResourceRegistration,
    ResourceDescriptor,
    text_agent_card,
    validate_resource_descriptor,
)

REGISTRY_WORKFLOW_ID_PREFIX = "account-registry"
REGISTRY_TASK_QUEUE = "mcp-registry"
GLOBAL_CATALOG_WORKFLOW_ID = "global-resource-catalog"


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
    """Account-owned resources, all expressed through the shared schema."""

    resources: dict[str, ResourceDescriptor] = field(default_factory=dict)

    @property
    def remote_servers(self) -> dict[str, str]:
        return {
            key: item.endpoint
            for key, item in self.resources.items()
            if item.category == "mcp" and item.transport == "external_http"
        }

    @property
    def nexus_servers(self) -> dict[str, ResourceDescriptor]:
        return {
            key: item
            for key, item in self.resources.items()
            if item.category == "mcp" and item.transport == "nexus"
        }

    @property
    def subagent_providers(self) -> dict[str, str]:
        return {
            key: item.endpoint
            for key, item in self.resources.items()
            if item.category == "agent" and item.transport == "external_http"
        }


@workflow.defn(sandboxed=False, name="GlobalResourceCatalog")
class GlobalCatalogWorkflow:
    """Prototype global catalog of resources available for account installation."""

    def __init__(self) -> None:
        self._resources: dict[str, ResourceDescriptor] = {}

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.update
    def publish_resources(
        self, descriptors: list[ResourceDescriptor]
    ) -> list[ResourceDescriptor]:
        for descriptor in descriptors:
            try:
                validate_resource_descriptor(descriptor)
            except ValueError as exc:
                raise ApplicationError(
                    str(exc), type="InvalidCatalogResource", non_retryable=True
                ) from exc
            current = self._resources.get(descriptor.resource_id)
            if current is not None and descriptor.revision < current.revision:
                raise ApplicationError(
                    f"catalog resource {descriptor.resource_id!r} cannot move from "
                    f"revision {current.revision} back to {descriptor.revision}",
                    type="CatalogRevisionRegression",
                    non_retryable=True,
                )
            self._resources[descriptor.resource_id] = descriptor
        return list(self._resources.values())

    @workflow.query
    def list_resources(self) -> list[ResourceDescriptor]:
        return list(self._resources.values())

    @workflow.query
    def get_resource(self, resource_id: str) -> ResourceDescriptor | None:
        return self._resources.get(resource_id)


def AgentRegistration(
    agent_id: str,
    kind: str,
    label: str,
    description: str,
    nexus_endpoint: str | None = None,
    nexus_service: str = "A2AService",
    provider_url: str | None = None,
) -> ResourceDescriptor:
    """Build a canonical agent descriptor for programmatic registration."""
    if kind not in {"harness_nexus", "external_http"}:
        raise ValueError(f"unsupported agent kind {kind!r}")
    native = kind == "harness_nexus"
    return ResourceDescriptor(
        resource_id=agent_id,
        revision=1,
        category="agent",
        transport="nexus" if native else "external_http",
        label=label,
        description=description,
        endpoint=(nexus_endpoint if native else provider_url) or "",
        service=nexus_service if native else None,
        agent_card=text_agent_card(
            name=label,
            description=description,
            endpoint=(nexus_endpoint if native else provider_url) or "",
            transport="nexus" if native else "external_http",
        ),
    )


def NexusMCPServerRegistration(
    name: str, endpoint: str, service: str
) -> ResourceDescriptor:
    """Build a canonical Nexus MCP descriptor for programmatic registration."""
    return ResourceDescriptor(
        resource_id=name,
        revision=1,
        category="mcp",
        transport="nexus",
        label=name,
        description="Nexus-native MCP server.",
        endpoint=endpoint,
        service=service,
    )


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
    has_started: bool = False


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
        self._resources: dict[str, AccountResourceRegistration] = {}
        self._subagent_instances: dict[str, SubagentInstanceRoute] = {}
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
        self._install(
            ResourceDescriptor(
                resource_id=name,
                revision=1,
                category="mcp",
                transport="external_http",
                label=name,
                description="Externally hosted MCP server.",
                endpoint=url,
            )
        )
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
        if self._resources.pop(name, None) is not None:
            workflow.logger.info("[registry] deregistered %r", name)

    @workflow.signal
    def clear_all(self) -> None:
        """Remove all MCP server and subagent registrations."""
        self._resources.clear()
        self._subagent_instances.clear()
        self._sessions.clear()
        self._session_events.clear()
        workflow.logger.info("[registry] cleared all entries")

    @workflow.signal
    def register_subagent(self, alias: str, url: str) -> None:
        """Register a minimal HTTP subagent provider for this account."""
        self._install(
            ResourceDescriptor(
                resource_id=alias,
                revision=1,
                category="agent",
                transport="external_http",
                label=alias,
                description="Externally hosted HTTP agent.",
                endpoint=url,
                agent_card=text_agent_card(
                    name=alias,
                    description="Externally hosted A2A agent.",
                    endpoint=url,
                    transport="external_http",
                ),
            )
        )
        workflow.logger.info("[registry] registered subagent %r at %s", alias, url)

    @workflow.signal
    def deregister_subagent(self, alias: str) -> None:
        """Remove one account-owned subagent provider."""
        if self._resources.pop(alias, None) is not None:
            workflow.logger.info("[registry] deregistered subagent %r", alias)

    @workflow.update
    def register_nexus_mcp_server(
        self, descriptor: ResourceDescriptor
    ) -> ResourceDescriptor:
        """Register metadata without changing the service's direct Nexus route."""
        if descriptor.category != "mcp" or descriptor.transport != "nexus":
            raise ApplicationError(
                "register_nexus_mcp_server requires a Nexus MCP descriptor",
                type="InvalidNexusMCPRegistration",
                non_retryable=True,
            )
        return self._install(descriptor).descriptor

    @workflow.update
    def register_agent(self, descriptor: ResourceDescriptor) -> ResourceDescriptor:
        if descriptor.category != "agent":
            raise ApplicationError(
                "register_agent requires an agent descriptor",
                type="InvalidAgentRegistration",
                non_retryable=True,
            )
        return self._install(descriptor).descriptor

    def _install(self, descriptor: ResourceDescriptor) -> AccountResourceRegistration:
        try:
            validate_resource_descriptor(descriptor)
        except ValueError as exc:
            raise ApplicationError(
                str(exc), type="InvalidResourceRegistration", non_retryable=True
            ) from exc
        try:
            installed_at = workflow.time()
        except Exception:  # noqa: BLE001 - direct deterministic unit construction
            installed_at = 0.0
        registration = AccountResourceRegistration(
            descriptor=descriptor,
            installed_at=installed_at,
        )
        self._resources[descriptor.resource_id] = registration
        return registration

    @workflow.update
    def install_resource(
        self, descriptor: ResourceDescriptor
    ) -> AccountResourceRegistration:
        """Install a pinned catalog descriptor into this account."""
        return self._install(descriptor)

    @workflow.update
    def remove_resource(self, resource_id: str) -> None:
        """Disable future discovery while preserving retained session records."""
        registration = self._resources.get(resource_id)
        if registration is not None:
            self._resources[resource_id] = replace(registration, enabled=False)

    @workflow.update
    def deregister_agent(self, agent_id: str) -> None:
        self._resources.pop(agent_id, None)

    @workflow.update
    def create_session(
        self,
        agent_id: str,
        provider_session_id: str | None = None,
        is_message_queuing_enabled: bool = False,
    ) -> SessionRecord:
        resource = self._resource(agent_id)
        if resource is None or resource.category != "agent":
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
        *,
        authoritative_started: bool = False,
    ) -> SessionRecord | None:
        parent = self._require_session(parent_session_id)
        registration = self._resource(observation.agent_key)
        if registration is None:
            workflow.logger.info(
                "[registry] ignoring unregistered spawned agent %r from session %r",
                observation.agent_key,
                parent_session_id,
            )
            return None

        provider_session_id = observation.provider_session_id
        if registration.transport == "external_http":
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
        has_started = observation.has_started or current_turn > 0
        existing = self._spawned_session(
            parent_session_id, observation.provider_session_id
        )
        if existing is not None:
            updated = replace(
                existing,
                has_started=(
                    has_started
                    if authoritative_started
                    else existing.has_started or has_started
                ),
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
            has_started=has_started,
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
            session = self._observe_spawned_agent(
                parent_session_id,
                observation,
                authoritative_started=True,
            )
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
    def bind_session_provider(
        self, session_id: str, provider_session_id: str
    ) -> SessionRecord:
        """Record the A2A Task ID assigned by an external agent on first send."""
        if not provider_session_id:
            raise ValueError("provider_session_id is required")
        session = self._require_session(session_id)
        updated = replace(session, provider_session_id=provider_session_id)
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

    def _resource(self, resource_id: str) -> ResourceDescriptor | None:
        registration = self._resources.get(resource_id)
        if registration is None or not registration.enabled:
            return None
        return registration.descriptor

    @workflow.query
    def find(self, name: str) -> str | None:
        resource = self._resource(name)
        if (
            resource is None
            or resource.category != "mcp"
            or resource.transport != "external_http"
        ):
            return None
        return resource.endpoint

    @workflow.query
    def find_subagent(self, alias: str) -> str | None:
        resource = self._resource(alias)
        if (
            resource is None
            or resource.category != "agent"
            or resource.transport != "external_http"
        ):
            return None
        return resource.endpoint

    @workflow.query
    def find_subagent_instance(self, instance_id: str) -> SubagentInstanceRoute | None:
        """Return the provider route for one gateway instance."""
        return self._subagent_instances.get(instance_id)

    @workflow.query
    def list_account_entries(self) -> AccountEntries:
        return AccountEntries(
            resources={
                key: registration.descriptor
                for key, registration in self._resources.items()
                if registration.enabled
            }
        )

    @workflow.query
    def list_agents(self) -> list[ResourceDescriptor]:
        return [
            registration.descriptor
            for registration in self._resources.values()
            if registration.enabled and registration.descriptor.category == "agent"
        ]

    @workflow.query
    def get_agent(self, agent_id: str) -> ResourceDescriptor | None:
        registration = self._resources.get(agent_id)
        if registration is None or registration.descriptor.category != "agent":
            return None
        return registration.descriptor

    @workflow.query
    def list_sessions(self) -> list[SessionRecord]:
        return [
            self._normalized_session(session) for session in self._sessions.values()
        ]

    @workflow.query
    def get_session(self, session_id: str) -> SessionRecord | None:
        session = self._sessions.get(session_id)
        return self._normalized_session(session) if session is not None else None

    @staticmethod
    def _normalized_session(session: SessionRecord) -> SessionRecord:
        # A lazy spawned A2A route has no task before its first accepted turn.
        if session.is_spawned and session.has_started and session.current_turn == 0:
            return replace(session, has_started=False)
        return session

    @workflow.query
    def resolve_session(self, session_id: str) -> SessionRecord | None:
        """Resolve an account session by its public ID or registered provider alias."""
        session = self._sessions.get(session_id)
        if session is not None:
            return self._normalized_session(session)
        resolved = next(
            (
                candidate
                for candidate in self._sessions.values()
                if candidate.source_session_id == session_id
                or candidate.provider_session_id == session_id
            ),
            None,
        )
        return self._normalized_session(resolved) if resolved is not None else None

    @workflow.query
    def poll_session_events(
        self, session_id: str, from_offset: int
    ) -> list[SessionEvent]:
        return [
            event
            for event in self._session_events.get(session_id, [])
            if event.offset > from_offset
        ]
