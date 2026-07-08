# ABOUTME: Standalone (client-side) calls against SubagentService — turn_driver.py's calls, but
# via client.create_nexus_client since the gateway is a plain server process, not a workflow.
# Needs the standalone-Nexus server capability (see registration.py).

from __future__ import annotations

import base64
import json
import uuid
from datetime import timedelta

from temporalio.api.common.v1 import Payload as CommonPayload
from temporalio.client import Client

from temporal_agent_harness.harness.agent_protocol import AgentEvent

from subagents.transport import (
    CancelTaskInput,
    GetTaskInput,
    Message,
    PollTaskUpdatesInput,
    PollTaskUpdatesOutput,
    SendMessageInput,
    StreamItem,
    SubagentService,
    Task,
)

_OPERATION_TIMEOUT = timedelta(seconds=30)
# Long-poll window per pollTaskUpdates round trip when streaming (message/stream,
# tasks/resubscribe) — the loop re-polls immediately after each return, so this only bounds one
# round trip's server-side wait, not the overall streaming cadence.
_STREAM_POLL_TIMEOUT_SECONDS = 30.0


def _fresh_id(prefix: str) -> str:
    """Fresh id per call — not keyed by task_id alone, since reusing an id across different
    calls risks being treated as a retry of the first."""
    return f"{prefix}-{uuid.uuid4()}"


async def send_message(client: Client, endpoint: str, message: Message) -> Task:
    nexus_client = client.create_nexus_client(service=SubagentService, endpoint=endpoint)
    return await nexus_client.execute_operation(
        SubagentService.send_message,
        SendMessageInput(message=message),
        id=_fresh_id("gw-send"),
        schedule_to_close_timeout=_OPERATION_TIMEOUT,
    )


async def get_task(client: Client, endpoint: str, task_id: str) -> Task:
    nexus_client = client.create_nexus_client(service=SubagentService, endpoint=endpoint)
    return await nexus_client.execute_operation(
        SubagentService.get_task,
        GetTaskInput(task_id=task_id),
        id=_fresh_id("gw-get"),
        schedule_to_close_timeout=_OPERATION_TIMEOUT,
    )


async def cancel_task(client: Client, endpoint: str, task_id: str) -> Task:
    nexus_client = client.create_nexus_client(service=SubagentService, endpoint=endpoint)
    return await nexus_client.execute_operation(
        SubagentService.cancel_task,
        CancelTaskInput(task_id=task_id),
        id=_fresh_id("gw-cancel"),
        schedule_to_close_timeout=_OPERATION_TIMEOUT,
    )


def decode_stream_item(item: StreamItem) -> AgentEvent:
    """Same wire format as turn_driver.py/stream_source.py's decode helpers; duplicated since
    each caller has a different dependency footprint."""
    raw = base64.b64decode(item.data)
    payload = CommonPayload()
    payload.ParseFromString(raw)
    return AgentEvent.model_validate(json.loads(payload.data))


async def poll_task_updates(
    client: Client, endpoint: str, task_id: str, cursor: int
) -> PollTaskUpdatesOutput:
    nexus_client = client.create_nexus_client(service=SubagentService, endpoint=endpoint)
    return await nexus_client.execute_operation(
        SubagentService.poll_task_updates,
        PollTaskUpdatesInput(
            task_id=task_id, cursor=cursor, timeout_seconds=_STREAM_POLL_TIMEOUT_SECONDS
        ),
        id=_fresh_id("gw-poll"),
        schedule_to_close_timeout=_OPERATION_TIMEOUT
        + timedelta(seconds=_STREAM_POLL_TIMEOUT_SECONDS),
    )
