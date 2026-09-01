"""Account-specific external-agent actions and child discovery."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any

from a2a.types import StreamResponse
from google.protobuf.json_format import MessageToDict
from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.common import RetryPolicy

from .registry import REGISTRY_TASK_QUEUE, SpawnedAgentObservation

with workflow.unsafe.imports_passed_through():
    from nexus_a2a import A2AService, SubscribeToTaskInput, SubscribeToTaskItem
    from temporal_agent_harness.a2a.generated import (
        HarnessControlService,
        QuerySessionInput,
    )
    from temporal_agent_harness.a2a.stream import (
        HARNESS_EVENT_METADATA_KEY,
    )

    from .registry_service_handler import (
        SubagentDispatchInput,
        SubagentStartInput,
        SubagentStopInput,
        subagent_proxy_activity,
        subagent_start_activity,
        subagent_stop_activity,
    )

AGENT_DISCOVERY_WORKFLOW_NAME = "BrokeredAgentDiscovery"


@dataclass(frozen=True)
class ExternalAgentActionInput:
    action: str
    session_id: str
    provider_url: str
    values: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentDiscoveryInput:
    session_id: str
    nexus_endpoint: str
    cursor: int = 0


def _decode_stream_item(item: SubscribeToTaskItem) -> tuple[str, dict[str, Any]]:
    response = StreamResponse()
    response.ParseFromString(base64.b64decode(item.data))
    body = response.WhichOneof("payload")
    if body is None:
        raise ValueError("A2A StreamResponse has no payload")
    a2a_metadata = MessageToDict(
        getattr(response, body).metadata, preserving_proto_field_name=True
    )
    raw = base64.b64decode(str(a2a_metadata[HARNESS_EVENT_METADATA_KEY]))
    payload = Payload()
    payload.ParseFromString(raw)
    envelope = json.loads(payload.data)
    event = envelope["event"]
    event_type = event["type"]
    return event_type, {
        **event,
        "agent_id": envelope["agent_id"],
        "turn_id": envelope["turn_id"],
        "turn_number": envelope["turn_number"],
        "timestamp": envelope["timestamp"],
        "resume_offset": item.offset + 1,
    }


def _spawned_lifecycle(
    items: list[SubscribeToTaskItem],
) -> tuple[list[SpawnedAgentObservation], list[str]]:
    spawned_agents: list[SpawnedAgentObservation] = []
    stopped_source_session_ids: list[str] = []
    for item in items:
        event_type, data = _decode_stream_item(item)
        if event_type == "subagent_started":
            spawned_agents.append(
                SpawnedAgentObservation(
                    subagent_id=str(data["subagent_id"]),
                    agent_key=str(data["agent_key"]),
                    provider_session_id=str(data["workflow_id"]),
                )
            )
        elif event_type == "subagent_message_sent":
            spawned_agents.append(
                SpawnedAgentObservation(
                    subagent_id=str(data["subagent_id"]),
                    agent_key=str(data["agent_key"]),
                    provider_session_id=str(data["workflow_id"]),
                    next_expected_turn=int(data["subagent_turn"]) + 1,
                )
            )
        elif event_type == "subagent_stopped":
            stopped_source_session_ids.append(str(data["workflow_id"]))
    return spawned_agents, stopped_source_session_ids


def _observation_dict(observation: SpawnedAgentObservation) -> dict[str, Any]:
    return {
        "subagent_id": observation.subagent_id,
        "agent_key": observation.agent_key,
        "workflow_id": observation.provider_session_id,
        "next_expected_turn": observation.next_expected_turn,
    }


def _values(input: ExternalAgentActionInput) -> dict[str, Any]:
    return input.values or {}


async def execute_external_agent_action(
    client: Client,
    input: ExternalAgentActionInput,
    *,
    execution_id: str,
) -> dict[str, Any]:
    """Execute one third-party HTTP action as a standalone activity."""
    values = _values(input)
    if input.action == "start":
        result = await client.execute_activity(
            subagent_start_activity,
            SubagentStartInput(
                url=input.provider_url,
                idempotency_key=str(values["idempotency_key"]),
            ),
            id=execution_id,
            task_queue=REGISTRY_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=5),
            summary=f"start external agent at {input.provider_url}",
        )
        return result.model_dump(mode="json")
    if input.action == "send":
        result = await client.execute_activity(
            subagent_proxy_activity,
            SubagentDispatchInput(
                url=input.provider_url,
                instance_id=input.session_id,
                msg_type=str(values["msg_type"]),
                payload=json.dumps(values.get("payload") or {}),
                expected_turn=int(values["expected_turn"]),
                idempotency_key=str(values["idempotency_key"]),
            ),
            id=execution_id,
            task_queue=REGISTRY_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=75),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=5),
            summary=f"send turn to external agent session {input.session_id}",
        )
        return result.model_dump(mode="json")
    if input.action == "close":
        await client.execute_activity(
            subagent_stop_activity,
            SubagentStopInput(
                url=input.provider_url,
                instance_id=input.session_id,
            ),
            id=execution_id,
            task_queue=REGISTRY_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=RetryPolicy(maximum_attempts=5),
            summary=f"close external agent session {input.session_id}",
        )
        return {"closed": True}
    raise ValueError(f"external agents do not support {input.action!r}")


@workflow.defn(sandboxed=False, name=AGENT_DISCOVERY_WORKFLOW_NAME)
class AgentDiscoveryWorkflow:
    """Drain retained lifecycle events and reconcile a native agent's children."""

    @workflow.run
    async def run(self, input: AgentDiscoveryInput) -> dict[str, Any]:
        a2a_client = workflow.create_nexus_client(
            service=A2AService, endpoint=input.nexus_endpoint
        )
        control_client = workflow.create_nexus_client(
            service=HarnessControlService, endpoint=input.nexus_endpoint
        )
        cursor = input.cursor
        spawned: list[SpawnedAgentObservation] = []
        stopped: list[str] = []
        closed = False
        for _ in range(100):
            poll = await a2a_client.execute_operation(
                A2AService.subscribe_to_task,
                SubscribeToTaskInput(
                    id=input.session_id,
                    cursor=cursor,
                    timeout_seconds=0.1,
                ),
            )
            batch_spawned, batch_stopped = _spawned_lifecycle(poll.items)
            spawned.extend(batch_spawned)
            stopped.extend(batch_stopped)
            cursor = poll.next_cursor
            closed = poll.closed
            if poll.closed or not poll.more_ready:
                break
        active = []
        if not closed:
            status = await control_client.execute_operation(
                HarnessControlService.query_agent_status,
                QuerySessionInput(session_id=input.session_id),
            )
            active = [asdict(item) for item in status.subagents]
        return {
            "spawned": [_observation_dict(item) for item in spawned],
            "stopped_source_session_ids": stopped,
            "next_offset": cursor,
            "active": active,
        }
