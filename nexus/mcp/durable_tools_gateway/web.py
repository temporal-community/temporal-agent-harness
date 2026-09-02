"""Account-scoped FastAPI surface for the brokered agent UI."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from temporalio.api.workflowservice.v1 import DescribeActivityExecutionRequest
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import ActivitySerializationContext
from temporalio.envconfig import ClientConfig
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.ui import packaged_ui_dist
from temporal_agent_harness.utils.large_payload import with_large_payload_offload

from .agent_broker import (
    AGENT_ATTACH_WORKFLOW_NAME,
    AgentActionInput,
    AgentAttachInput,
    AgentAttachWorkflow,
    AgentDiscoveryInput,
    AgentDiscoveryWorkflow,
    event_broker,
    execute_agent_action,
)
from .registry import (
    GLOBAL_CATALOG_WORKFLOW_ID,
    REGISTRY_TASK_QUEUE,
    AccountEntries,
    GlobalCatalogWorkflow,
    PendingSessionEvent,
    SessionEvent,
    SessionRecord,
    SpawnedAgentObservation,
    ToolRegistryWorkflow,
    account_registry_workflow_id,
)
from .registry_service_handler import (
    SubagentDispatchInput,
    SubagentDispatchOutput,
    subagent_dispatch_activity_id,
)
from .resources import (
    TEXT_AGENT_HANDLER,
    AccountResourceRegistration,
    ResourceDescriptor,
)
from .tool_history import scan_external_tool_calls, scan_native_tool_calls

logger = logging.getLogger(__name__)
_EXTERNAL_EVENTS_PER_TURN = 4


class ExternalTurnHistoryUnavailable(Exception):
    """A retained third-party turn is no longer addressable in Temporal."""


class RegisterAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    kind: Literal["harness_nexus", "external_http"] = "harness_nexus"
    label: str
    description: str = ""
    nexus_endpoint: str | None = None
    provider_url: str | None = None


class RegisterNexusMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    endpoint: str
    service: str


class CreateSessionRequest(BaseModel):
    agent_workflow_type: str
    is_message_queuing_enabled: bool = False


class CloseSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subagent_close_policy: Literal["keep-open", "close"] | None = None


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    message: str | dict[str, Any]
    expected_turn: int


class ApprovalRequest(BaseModel):
    session_id: str
    tool_id: str
    approved: bool
    reason: str | None = None
    remember: bool = False


class OperatorCommandRequest(BaseModel):
    session_id: str
    name: str
    arg: str | None = None


class CallbackRequest(BaseModel):
    session_id: str
    tool_id: str
    result: Any = None
    error: str | None = None


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def _message_parts(message: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if isinstance(message, str):
        return "ask", {"text": message}
    return str(message.get("type", "ask")), dict(message.get("payload") or {})


def _session_response(session: SessionRecord) -> dict[str, Any]:
    return {
        "workflow_id": session.session_id,
        "created_at": session.created_at,
        "label": session.label,
        "agent_workflow_type": session.agent_id,
        "parent_session_id": session.parent_session_id,
        "subagent_id": session.subagent_id,
        "is_spawned": session.is_spawned,
        "is_message_queuing_enabled": session.is_message_queuing_enabled,
        "is_discovered": session.is_spawned,
        "execution_status": (
            "COMPLETED"
            if session.closed
            else "RUNNING"
            if session.has_started
            else "NOT_STARTED"
        ),
        "closed": session.closed,
    }


def _agent_response(agent: ResourceDescriptor) -> dict[str, Any]:
    return {
        "agent_id": agent.resource_id,
        "kind": agent.kind,
        "label": agent.label,
        "description": agent.description,
        "nexus_endpoint": agent.nexus_endpoint,
        "nexus_service": agent.nexus_service,
        "provider_url": agent.provider_url,
        "revision": agent.revision,
    }


def _empty_status(session: SessionRecord) -> dict[str, Any]:
    return {
        "agent_id": session.agent_id,
        "current_turn": session.current_turn,
        "turn_active": False,
        "pending_turns": [],
        "is_message_queuing_enabled": session.is_message_queuing_enabled,
        "pending_approvals": [],
        "pending_callbacks": [],
        "subagents": [],
        "approval_policy": {
            "dangerously_skip_all_approvals": False,
            "auto_approve_inherently_safe": False,
            "auto_approve_tools": [],
        },
        "has_custom_approval_fallback": False,
        "subagent_close_policy": "ask-user",
        "subagent_reuse_policy": "use-existing",
    }


def _normalize_status(value: dict[str, Any]) -> dict[str, Any]:
    for approval in value.get("pending_approvals", []):
        approval["tool_input"] = _json_value(approval.get("tool_input", ""))
    for callback in value.get("pending_callbacks", []):
        callback["tool_input"] = _json_value(callback.get("tool_input", ""))
        callback["output_schema"] = _json_value(callback.get("output_schema", ""))
    return value


def _sse(event_type: str, data: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _external_turn_events(
    *,
    agent_id: str,
    activity_input: SubagentDispatchInput,
    activity_output: SubagentDispatchOutput,
    started_at: float,
    completed_at: float,
) -> list[SessionEvent]:
    """Project one retained standalone activity into the UI's event protocol."""
    turn_number = activity_output.turn_number
    first_offset = (turn_number - 1) * _EXTERNAL_EVENTS_PER_TURN + 1
    try:
        payload = json.loads(activity_input.payload)
    except json.JSONDecodeError:
        payload = activity_input.payload
    output = _json_value(activity_output.output)
    text = (
        output.get("text", json.dumps(output))
        if isinstance(output, dict)
        else str(output)
    )
    common = {
        "agent_id": agent_id,
        "turn_id": activity_output.turn_id,
        "turn_number": turn_number,
    }
    return [
        SessionEvent(
            offset=first_offset,
            event_type="turn_started",
            data={
                **common,
                "timestamp": started_at,
                "type": "turn_started",
                "user_message": json.dumps(
                    {"type": activity_input.msg_type, "payload": payload}
                ),
            },
        ),
        SessionEvent(
            offset=first_offset + 1,
            event_type="reply_delta",
            data={
                **common,
                "timestamp": completed_at,
                "type": "reply_delta",
                "text": text,
            },
        ),
        SessionEvent(
            offset=first_offset + 2,
            event_type="reply",
            data={
                **common,
                "timestamp": completed_at,
                "type": "reply",
                "output": output,
            },
        ),
        SessionEvent(
            offset=first_offset + 3,
            event_type="turn_end",
            data={**common, "timestamp": completed_at, "type": "turn_end"},
        ),
    ]


async def _replay_external_activity_turn(
    client: Client,
    *,
    source_session_id: str,
    agent_id: str,
    turn_number: int,
) -> list[SessionEvent] | None:
    """Read one third-party turn directly from its retained standalone activity."""
    activity_id = subagent_dispatch_activity_id(source_session_id, turn_number)
    try:
        response = await client.workflow_service.describe_activity_execution(
            DescribeActivityExecutionRequest(
                namespace=client.namespace,
                activity_id=activity_id,
                include_input=True,
                include_outcome=True,
            ),
            retry=True,
        )
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            raise ExternalTurnHistoryUnavailable(activity_id) from exc
        raise

    if not response.HasField("outcome"):
        return None
    if response.outcome.HasField("failure"):
        failure = await client.data_converter.decode_failure(response.outcome.failure)
        raise ExternalTurnHistoryUnavailable(f"{activity_id}: {failure}")
    if not response.input.payloads or not response.outcome.result.payloads:
        raise ExternalTurnHistoryUnavailable(activity_id)

    info = response.info
    converter = client.data_converter.with_context(
        ActivitySerializationContext(
            namespace=client.namespace,
            activity_id=activity_id,
            activity_type=info.activity_type.name,
            activity_task_queue=info.task_queue,
            workflow_id=None,
            workflow_type=None,
            is_local=False,
        )
    )
    [activity_input] = await converter.decode(
        response.input.payloads, [SubagentDispatchInput]
    )
    [activity_output] = await converter.decode(
        response.outcome.result.payloads, [SubagentDispatchOutput]
    )
    if (
        activity_input.expected_turn != turn_number
        or activity_output.turn_number != turn_number
    ):
        raise ExternalTurnHistoryUnavailable(
            f"{activity_id}: retained turn does not match its activity ID"
        )
    return _external_turn_events(
        agent_id=agent_id,
        activity_input=activity_input,
        activity_output=activity_output,
        started_at=info.schedule_time.ToDatetime().timestamp(),
        completed_at=info.close_time.ToDatetime().timestamp(),
    )


def _mount_ui(app: FastAPI, static_path: Path) -> None:
    assets = static_path / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    app.mount("/static", StaticFiles(directory=static_path), name="static")

    @app.get("/")
    async def index():
        return FileResponse(static_path / "index.html")

    @app.get("/{asset_name}")
    async def top_level_asset(asset_name: str):
        path = static_path / asset_name
        if path.is_file():
            return FileResponse(path)
        raise HTTPException(status_code=404)


def create_account_agent_app(
    account_id: str,
    *,
    temporal_client: Client | None = None,
    static_dir: Path | str | None = None,
) -> FastAPI:
    """Create a UI/API whose authority is exactly one account registry."""
    if not account_id.strip():
        raise ValueError("account_id is required")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = temporal_client
        if client is None:
            config = ClientConfig.load_client_connect_config()
            client = await Client.connect(
                **config,
                data_converter=await with_large_payload_offload(
                    pydantic_data_converter
                ),
            )
        app.state.temporal = client
        app.state.registry = await client.start_workflow(
            ToolRegistryWorkflow.run,
            account_id,
            id=account_registry_workflow_id(account_id),
            task_queue=REGISTRY_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        app.state.catalog = await client.start_workflow(
            GlobalCatalogWorkflow.run,
            id=GLOBAL_CATALOG_WORKFLOW_ID,
            task_queue=REGISTRY_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        yield

    app = FastAPI(lifespan=lifespan)

    async def registry_handle() -> WorkflowHandle[Any, Any]:
        return app.state.registry

    async def resolve_session(
        session_id: str,
    ) -> tuple[SessionRecord, ResourceDescriptor]:
        handle = await registry_handle()
        session: SessionRecord | None = await handle.query(
            ToolRegistryWorkflow.resolve_session,
            session_id,
            result_type=SessionRecord | None,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="unknown account session")
        agent: ResourceDescriptor | None = await handle.query(
            ToolRegistryWorkflow.get_agent,
            session.agent_id,
            result_type=ResourceDescriptor | None,
        )
        if agent is None:
            raise HTTPException(
                status_code=409, detail="session agent is no longer registered"
            )
        return session, agent

    async def execute_action(
        input: AgentActionInput,
    ) -> dict[str, Any]:
        return await execute_agent_action(
            app.state.temporal,
            input,
            execution_id=f"ui-agent-{input.action}-{uuid.uuid4()}",
        )

    async def submit(req: MessageRequest) -> dict[str, Any]:
        session, agent = await resolve_session(req.session_id)
        if session.closed:
            raise HTTPException(status_code=409, detail="session is closed")
        msg_type, payload = _message_parts(req.message)
        expected_turn = (
            session.current_turn + 1 if agent.kind == "external_http" else None
        )
        values = {
            "msg_type": msg_type,
            "payload": payload,
            "account_id": account_id,
            "registered_agent_id": agent.resource_id,
            "delegation_lineage": [],
            "delegation_depth": 0,
            "max_delegation_depth": 5,
            # Native AgentService reconciles its live turn when this is omitted.
            "expected_turn": expected_turn,
            "idempotency_key": (f"{account_id}:{session.session_id}:{expected_turn}"),
        }
        result = await execute_action(
            AgentActionInput(
                action="send",
                session_id=session.provider_session_id,
                nexus_endpoint=agent.nexus_endpoint,
                provider_url=agent.provider_url,
                values=values,
            )
        )
        handle = await registry_handle()
        if agent.kind == "external_http":
            turn_number = int(result["turn_number"])
            turn_id = str(result["turn_id"])
            output = _json_value(str(result["output"]))
            text = (
                output.get("text", json.dumps(output))
                if isinstance(output, dict)
                else str(output)
            )
            timestamp = time.time()
            metadata = {
                "agent_id": agent.agent_id,
                "turn_id": turn_id,
                "turn_number": turn_number,
                "timestamp": timestamp,
            }
            events = [
                PendingSessionEvent(
                    "turn_started",
                    {
                        **metadata,
                        "type": "turn_started",
                        "user_message": json.dumps(
                            {"type": msg_type, "payload": payload}
                        ),
                    },
                ),
                PendingSessionEvent(
                    "reply_delta", {**metadata, "type": "reply_delta", "text": text}
                ),
                PendingSessionEvent(
                    "reply", {**metadata, "type": "reply", "output": output}
                ),
                PendingSessionEvent("turn_end", {**metadata, "type": "turn_end"}),
            ]
            await handle.execute_update(
                ToolRegistryWorkflow.append_session_events,
                args=[session.session_id, events],
                result_type=int,
            )
            # Registry offsets are storage-local. The UI stream uses a stable
            # four-event window per external turn so retained standalone
            # activities and later UI-originated turns share one cursor space.
            accepted_offset: int = (turn_number - 1) * _EXTERNAL_EVENTS_PER_TURN
            pending = False
        else:
            turn_number = int(result["turn_number"])
            turn_id = str(result["turn_id"])
            accepted_offset = int(result.get("stream_head_offset") or 0)
            pending = bool(result.get("pending", False))
        await handle.execute_update(
            ToolRegistryWorkflow.mark_session_started,
            args=[session.session_id, turn_number],
            result_type=SessionRecord,
        )
        return {
            "turn_number": turn_number,
            "turn_id": turn_id,
            "accepted_offset": accepted_offset,
            "pending": pending,
        }

    @app.get("/api/agents")
    async def list_agents():
        agents: list[ResourceDescriptor] = await app.state.registry.query(
            ToolRegistryWorkflow.list_agents,
            result_type=list[ResourceDescriptor],
        )
        return {
            "agents": [
                {
                    "key": agent.agent_id,
                    "workflow_type": agent.agent_id,
                    "task_queue": "",
                    "label": agent.label,
                    "description": agent.description,
                }
                for agent in agents
            ]
        }

    @app.get("/api/account")
    async def account_overview():
        agents: list[ResourceDescriptor] = await app.state.registry.query(
            ToolRegistryWorkflow.list_agents,
            result_type=list[ResourceDescriptor],
        )
        sessions: list[SessionRecord] = await app.state.registry.query(
            ToolRegistryWorkflow.list_sessions,
            result_type=list[SessionRecord],
        )
        entries: AccountEntries = await app.state.registry.query(
            ToolRegistryWorkflow.list_account_entries,
            result_type=AccountEntries,
        )
        return {
            "account_id": account_id,
            "agents": [
                {
                    **_agent_response(agent),
                    "session_count": sum(
                        session.agent_id == agent.agent_id for session in sessions
                    ),
                    "active_session_count": sum(
                        session.agent_id == agent.agent_id and not session.closed
                        for session in sessions
                    ),
                }
                for agent in agents
            ],
            "mcp_servers": [
                {
                    "name": registration.name,
                    "endpoint": registration.endpoint,
                    "kind": "nexus",
                    "service": registration.service,
                }
                for registration in entries.nexus_servers.values()
            ]
            + [
                {
                    "name": name,
                    "endpoint": endpoint,
                    "kind": "external_http",
                    "service": None,
                }
                for name, endpoint in entries.remote_servers.items()
            ],
            "subagent_providers": [
                {"name": name, "endpoint": endpoint}
                for name, endpoint in entries.subagent_providers.items()
            ],
            "session_count": len(sessions),
            "active_session_count": sum(not session.closed for session in sessions),
        }

    @app.get("/api/catalog")
    async def catalog_overview():
        catalog: list[ResourceDescriptor] = await app.state.catalog.query(
            GlobalCatalogWorkflow.list_resources,
            result_type=list[ResourceDescriptor],
        )
        entries: AccountEntries = await app.state.registry.query(
            ToolRegistryWorkflow.list_account_entries,
            result_type=AccountEntries,
        )
        installed = set(entries.resources)
        return {
            "resources": [
                {
                    **descriptor.to_dict(),
                    "installed": descriptor.resource_id in installed,
                }
                for descriptor in catalog
            ]
        }

    @app.post("/api/catalog/{resource_id}/register")
    async def install_catalog_resource(resource_id: str):
        descriptor: ResourceDescriptor | None = await app.state.catalog.query(
            GlobalCatalogWorkflow.get_resource,
            resource_id,
            result_type=ResourceDescriptor | None,
        )
        if descriptor is None:
            raise HTTPException(status_code=404, detail="unknown catalog resource")
        registration: AccountResourceRegistration = (
            await app.state.registry.execute_update(
                ToolRegistryWorkflow.install_resource,
                descriptor,
                id=f"install-{resource_id}-r{descriptor.revision}-{uuid.uuid4()}",
                result_type=AccountResourceRegistration,
            )
        )
        return {
            **registration.descriptor.to_dict(),
            "installed": True,
            "installed_at": registration.installed_at,
        }

    @app.delete("/api/catalog/{resource_id}/register")
    async def remove_catalog_resource(resource_id: str):
        await app.state.registry.execute_update(
            ToolRegistryWorkflow.remove_resource,
            resource_id,
            id=f"remove-{resource_id}-{uuid.uuid4()}",
        )
        return {"resource_id": resource_id, "installed": False}

    @app.post("/api/account/agents")
    async def register_agent(req: RegisterAgentRequest):
        descriptor = ResourceDescriptor(
            resource_id=req.agent_id,
            revision=1,
            category="agent",
            transport="nexus" if req.kind == "harness_nexus" else "external_http",
            label=req.label,
            description=req.description,
            endpoint=req.nexus_endpoint or req.provider_url or "",
            service="AgentService" if req.kind == "harness_nexus" else None,
            handlers=(TEXT_AGENT_HANDLER,),
        )
        result: ResourceDescriptor = await app.state.registry.execute_update(
            ToolRegistryWorkflow.register_agent,
            descriptor,
            result_type=ResourceDescriptor,
        )
        return _agent_response(result)

    @app.post("/api/account/mcp-servers")
    async def register_nexus_mcp_server(req: RegisterNexusMCPServerRequest):
        descriptor = ResourceDescriptor(
            resource_id=req.name,
            revision=1,
            category="mcp",
            transport="nexus",
            label=req.name,
            description="Nexus-native MCP server.",
            endpoint=req.endpoint,
            service=req.service,
        )
        result: ResourceDescriptor = await app.state.registry.execute_update(
            ToolRegistryWorkflow.register_nexus_mcp_server,
            descriptor,
            result_type=ResourceDescriptor,
        )
        return {
            "name": result.resource_id,
            "endpoint": result.endpoint,
            "service": result.service,
        }

    @app.get("/api/mcp-servers/{server_name}/tool-calls")
    async def tool_calls(server_name: str):
        entries: AccountEntries = await app.state.registry.query(
            ToolRegistryWorkflow.list_account_entries,
            result_type=AccountEntries,
        )
        native = entries.nexus_servers.get(server_name)
        if native is not None:
            agents: list[ResourceDescriptor] = await app.state.registry.query(
                ToolRegistryWorkflow.list_agents,
                result_type=list[ResourceDescriptor],
            )
            sessions: list[SessionRecord] = await app.state.registry.query(
                ToolRegistryWorkflow.list_sessions,
                result_type=list[SessionRecord],
            )
            calls = await scan_native_tool_calls(
                app.state.temporal,
                server=native,
                agents=agents,
                sessions=sessions,
            )
        elif server_name in entries.remote_servers:
            calls = await scan_external_tool_calls(
                app.state.temporal,
                account_id=account_id,
                server_name=server_name,
            )
        else:
            raise HTTPException(status_code=404, detail="unknown registered MCP server")
        return [call.model_dump(mode="json") for call in calls]

    @app.get("/api/sessions")
    async def list_sessions():
        sessions: list[SessionRecord] = await app.state.registry.query(
            ToolRegistryWorkflow.list_sessions,
            result_type=list[SessionRecord],
        )
        return [_session_response(session) for session in sessions]

    @app.post("/api/sessions/refresh")
    async def refresh_sessions():
        """Reconcile registered children from live parent status snapshots."""
        handle = await registry_handle()
        sessions: list[SessionRecord] = await handle.query(
            ToolRegistryWorkflow.list_sessions,
            result_type=list[SessionRecord],
        )
        agents: list[ResourceDescriptor] = await handle.query(
            ToolRegistryWorkflow.list_agents,
            result_type=list[ResourceDescriptor],
        )
        agents_by_id = {agent.agent_id: agent for agent in agents}

        refresh_limit = asyncio.Semaphore(8)

        def observations(values: list[dict[str, Any]]) -> list[SpawnedAgentObservation]:
            return [
                SpawnedAgentObservation(
                    subagent_id=str(item["subagent_id"]),
                    agent_key=str(item["agent_key"]),
                    provider_session_id=str(item["workflow_id"]),
                    next_expected_turn=int(item.get("next_expected_turn", 1)),
                )
                for item in values
            ]

        async def sync_parent(session: SessionRecord) -> None:
            agent = agents_by_id.get(session.agent_id)
            if (
                agent is None
                or agent.kind != "harness_nexus"
                or session.closed
                or not session.has_started
            ):
                return
            try:
                async with refresh_limit:
                    result = await app.state.temporal.execute_workflow(
                        AgentDiscoveryWorkflow.run,
                        AgentDiscoveryInput(
                            session_id=session.provider_session_id,
                            nexus_endpoint=str(agent.nexus_endpoint),
                            cursor=session.discovery_offset,
                        ),
                        id=f"broker-discovery-{uuid.uuid4()}",
                        task_queue=REGISTRY_TASK_QUEUE,
                        result_type=dict[str, Any],
                    )
                await handle.execute_update(
                    ToolRegistryWorkflow.reconcile_spawned_agents,
                    args=[
                        session.session_id,
                        observations(result.get("spawned", [])),
                        result.get("stopped_source_session_ids", []),
                        int(result.get("next_offset", session.discovery_offset)),
                        observations(result.get("active", [])),
                    ],
                    result_type=list[SessionRecord],
                )
            except Exception as exc:  # noqa: BLE001 - isolate unavailable agents
                logger.warning(
                    "Could not refresh spawned sessions for %s: %s",
                    session.session_id,
                    exc,
                )
                return

        await asyncio.gather(*(sync_parent(session) for session in sessions))
        refreshed: list[SessionRecord] = await handle.query(
            ToolRegistryWorkflow.list_sessions,
            result_type=list[SessionRecord],
        )
        return [_session_response(session) for session in refreshed]

    @app.post("/api/sessions")
    async def create_session(req: CreateSessionRequest):
        agent: ResourceDescriptor | None = await app.state.registry.query(
            ToolRegistryWorkflow.get_agent,
            req.agent_workflow_type,
            result_type=ResourceDescriptor | None,
        )
        if agent is None:
            raise HTTPException(status_code=422, detail="unknown registered agent")
        provider_session_id = None
        if agent.kind == "external_http":
            result = await execute_action(
                AgentActionInput(
                    action="start",
                    session_id="",
                    provider_url=agent.provider_url,
                    values={"idempotency_key": f"{account_id}:{uuid.uuid4()}"},
                )
            )
            provider_session_id = str(result["instance_id"])
        session: SessionRecord = await app.state.registry.execute_update(
            ToolRegistryWorkflow.create_session,
            args=[
                agent.agent_id,
                provider_session_id,
                req.is_message_queuing_enabled,
            ],
            result_type=SessionRecord,
        )
        return _session_response(session)

    @app.get("/api/workflow-status/{session_id}")
    async def workflow_status(session_id: str):
        session, _ = await resolve_session(session_id)
        return {
            "workflow_id": session.session_id,
            "execution_status": (
                "COMPLETED"
                if session.closed
                else "RUNNING"
                if session.has_started
                else "NOT_STARTED"
            ),
            "closed": session.closed,
        }

    @app.get("/api/status/{session_id}")
    async def status(session_id: str):
        session, agent = await resolve_session(session_id)
        if agent.kind == "external_http" or not session.has_started:
            return _empty_status(session)
        result = await execute_action(
            AgentActionInput(
                action="status",
                session_id=session.provider_session_id,
                nexus_endpoint=agent.nexus_endpoint,
            )
        )
        return _normalize_status(result)

    @app.get("/api/agent-interface/{session_id}")
    async def agent_interface(session_id: str):
        session, agent = await resolve_session(session_id)
        if agent.kind == "external_http" or not session.has_started:
            return [
                {
                    "name": "ask",
                    "description": "Send a text message to this agent.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "output": {"type": "object"},
                }
            ]
        result = await execute_action(
            AgentActionInput(
                action="agent_interface",
                session_id=session.provider_session_id,
                nexus_endpoint=agent.nexus_endpoint,
            )
        )
        return [
            {
                **handler,
                "parameters": _json_value(handler["parameters"]),
                "output": _json_value(handler["output"]),
            }
            for handler in result["handlers"]
        ]

    @app.get("/api/operator-interface/{session_id}")
    async def operator_interface(session_id: str):
        session, agent = await resolve_session(session_id)
        if agent.kind == "external_http" or not session.has_started:
            return []
        result = await execute_action(
            AgentActionInput(
                action="operator_interface",
                session_id=session.provider_session_id,
                nexus_endpoint=agent.nexus_endpoint,
            )
        )
        return [
            {
                **command,
                "payload_name": command["name"],
                "aliases": [],
            }
            for command in result["commands"]
        ]

    async def external_stream(
        session: SessionRecord, from_offset: int
    ) -> AsyncIterator[bytes]:
        source_session_id = session.source_session_id
        if not session.is_spawned or not source_session_id:
            events: list[SessionEvent] = await app.state.registry.query(
                ToolRegistryWorkflow.poll_session_events,
                args=[session.session_id, from_offset],
                result_type=list[SessionEvent],
            )
            for event in events:
                yield _sse(
                    event.event_type,
                    {**event.data, "resume_offset": event.offset},
                )
            return

        retained_events: list[SessionEvent] = await app.state.registry.query(
            ToolRegistryWorkflow.poll_session_events,
            args=[session.session_id, 0],
            result_type=list[SessionEvent],
        )
        retained_by_turn: dict[int, list[SessionEvent]] = {}
        for event in retained_events:
            turn_number = int(event.data.get("turn_number") or 0)
            if turn_number > 0:
                retained_by_turn.setdefault(turn_number, []).append(event)

        replayed_by_turn: dict[int, list[SessionEvent] | Exception] = {}
        missing_turns = [
            turn_number
            for turn_number in range(1, session.current_turn + 1)
            if turn_number not in retained_by_turn
        ]
        replay_limit = asyncio.Semaphore(8)

        async def replay(
            turn_number: int,
        ) -> tuple[int, list[SessionEvent] | Exception]:
            try:
                async with replay_limit:
                    events = await _replay_external_activity_turn(
                        app.state.temporal,
                        source_session_id=source_session_id,
                        agent_id=session.agent_id,
                        turn_number=turn_number,
                    )
                return turn_number, events or []
            except ExternalTurnHistoryUnavailable as exc:
                return turn_number, exc
            except Exception as exc:  # noqa: BLE001 - keep one bad read isolated
                logger.warning(
                    "Could not replay external session %s turn %s: %s",
                    session.session_id,
                    turn_number,
                    exc,
                )
                return turn_number, exc

        replayed_by_turn.update(
            await asyncio.gather(*(replay(turn) for turn in missing_turns))
        )

        for turn_number in range(1, session.current_turn + 1):
            replayed = replayed_by_turn.get(turn_number)
            if isinstance(replayed, Exception):
                message = (
                    f"Turn {turn_number} history is no longer available from "
                    "Temporal retention."
                    if isinstance(replayed, ExternalTurnHistoryUnavailable)
                    else f"Could not load turn {turn_number} history from Temporal."
                )
                yield _sse(
                    "error",
                    {
                        "kind": "agent",
                        "message": message,
                        "resume_offset": from_offset,
                    },
                )
                continue

            events = retained_by_turn.get(turn_number, replayed or [])
            first_offset = (turn_number - 1) * _EXTERNAL_EVENTS_PER_TURN + 1
            for index, event in enumerate(sorted(events, key=lambda item: item.offset)):
                canonical_offset = first_offset + index
                if canonical_offset <= from_offset:
                    continue
                yield _sse(
                    event.event_type,
                    {**event.data, "resume_offset": canonical_offset},
                )

    async def native_stream(
        session: SessionRecord,
        agent: ResourceDescriptor,
        from_offset: int,
    ) -> AsyncIterator[bytes]:
        assert agent.nexus_endpoint is not None
        stream_id = uuid.uuid4().hex
        queue = event_broker.subscribe(stream_id)
        try:
            await app.state.temporal.start_workflow(
                AgentAttachWorkflow.run,
                AgentAttachInput(
                    nexus_endpoint=agent.nexus_endpoint,
                    session_id=session.provider_session_id,
                    stream_id=stream_id,
                    from_offset=from_offset,
                    registry_workflow_id=account_registry_workflow_id(account_id),
                    account_session_id=session.session_id,
                ),
                id=f"broker-attach-{stream_id}",
                task_queue=REGISTRY_TASK_QUEUE,
            )
            while True:
                frame = await queue.get()
                if frame is None:
                    return
                yield frame
        finally:
            event_broker.unsubscribe(stream_id, queue)

    @app.get("/api/attach")
    async def attach(session_id: str, from_offset: int = 0):
        session, agent = await resolve_session(session_id)
        if not session.has_started:
            stream: AsyncIterator[bytes] = external_stream(session, from_offset)
        elif agent.kind == "external_http":
            stream = external_stream(session, from_offset)
        else:
            stream = native_stream(session, agent, from_offset)
        return StreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/messages")
    async def messages(req: MessageRequest):
        return await submit(req)

    @app.post("/api/chat")
    async def chat(req: MessageRequest):
        result = await submit(req)
        session, agent = await resolve_session(req.session_id)
        stream = (
            external_stream(session, result["accepted_offset"])
            if agent.kind == "external_http"
            else native_stream(session, agent, result["accepted_offset"])
        )
        return StreamingResponse(stream, media_type="text/event-stream")

    @app.post("/api/approve")
    async def approve(req: ApprovalRequest):
        session, agent = await resolve_session(req.session_id)
        if agent.kind != "harness_nexus":
            raise HTTPException(
                status_code=501, detail="external agent approvals are unsupported"
            )
        return await execute_action(
            AgentActionInput(
                action="approve",
                session_id=session.provider_session_id,
                nexus_endpoint=agent.nexus_endpoint,
                values=req.model_dump(exclude={"session_id"}),
            )
        )

    @app.post("/api/callback-result")
    async def callback(req: CallbackRequest):
        session, agent = await resolve_session(req.session_id)
        if agent.kind != "harness_nexus":
            raise HTTPException(
                status_code=501, detail="external agent callbacks are unsupported"
            )
        return await execute_action(
            AgentActionInput(
                action="callback",
                session_id=session.provider_session_id,
                nexus_endpoint=agent.nexus_endpoint,
                values=req.model_dump(exclude={"session_id"}),
            )
        )

    @app.post("/api/operator-commands")
    async def operator_command(req: OperatorCommandRequest):
        session, agent = await resolve_session(req.session_id)
        if agent.kind != "harness_nexus":
            raise HTTPException(
                status_code=501, detail="external operator commands are unsupported"
            )
        result = await execute_action(
            AgentActionInput(
                action="operator_command",
                session_id=session.provider_session_id,
                nexus_endpoint=agent.nexus_endpoint,
                values=req.model_dump(exclude={"session_id"}),
            )
        )
        return {"text": result["reply"]}

    @app.post("/api/sessions/{session_id}/close")
    async def close(session_id: str, req: CloseSessionRequest | None = None):
        session, agent = await resolve_session(session_id)
        if session.has_started:
            resolution = req.subagent_close_policy if req is not None else None
            if agent.kind == "harness_nexus":
                if resolution is None:
                    status = _normalize_status(
                        await execute_action(
                            AgentActionInput(
                                action="status",
                                session_id=session.provider_session_id,
                                nexus_endpoint=agent.nexus_endpoint,
                            )
                        )
                    )
                    if (
                        status.get("subagent_close_policy") == "ask-user"
                        and status.get("subagents")
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "subagent_close_decision_required",
                                "session_id": session.session_id,
                                "subagents": status["subagents"],
                            },
                        )
                else:
                    await execute_action(
                        AgentActionInput(
                            action="operator_command",
                            session_id=session.provider_session_id,
                            nexus_endpoint=agent.nexus_endpoint,
                            values={
                                "name": "subagent-close-policy",
                                "arg": resolution,
                            },
                        )
                    )
            await execute_action(
                AgentActionInput(
                    action="close",
                    session_id=session.provider_session_id,
                    nexus_endpoint=agent.nexus_endpoint,
                    provider_url=agent.provider_url,
                )
            )
        await app.state.registry.execute_update(
            ToolRegistryWorkflow.close_session,
            session.session_id,
            result_type=SessionRecord,
        )
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    static_path = Path(static_dir) if static_dir is not None else packaged_ui_dist()
    if static_path is not None:
        _mount_ui(app, static_path)
    return app


__all__ = [
    "AGENT_ATTACH_WORKFLOW_NAME",
    "create_account_agent_app",
]
