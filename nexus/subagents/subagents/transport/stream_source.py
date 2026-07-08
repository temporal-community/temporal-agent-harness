# ABOUTME: nexus_remote_stream_source() — StreamSource factory that mounts a Nexus-routed
# subagent's stream in the client-side "own and attach" merge, by polling
# SubagentService.poll_task_updates instead of subscribing via WorkflowStreamClient.
#
# Needs the standalone-Nexus server capability (client.create_nexus_client from non-workflow
# code) — same requirement as registration.py's register/deregister, not yet in a released
# server. Without it, a Nexus-routed child's stream is just unavailable (graceful degradation
# in merge.py), not a crash.

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import timedelta

from temporalio.api.common.v1 import Payload as CommonPayload
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamItem

from temporal_agent_harness.harness.agent_protocol import TURN_EVENTS_TOPIC, AgentEvent
from temporal_agent_harness.harness.stream_merge import StreamSource

from .nexus_agent_service import PollTaskUpdatesInput, StreamItem, SubagentService

# Server-side long-poll window per round trip; the loop re-polls immediately after.
_POLL_TIMEOUT_SECONDS = 30.0

# Guards a tight loop if a poll ever returns empty and not closed.
_EMPTY_POLL_COOLDOWN = timedelta(milliseconds=50)


def _decode_stream_item(item: StreamItem) -> AgentEvent:
    """Same wire format as turn_driver.py's _decode_stream_item; duplicated since this module
    is plain client-side code, not workflow-sandboxed."""
    raw = base64.b64decode(item.data)
    payload = CommonPayload()
    payload.ParseFromString(raw)
    return AgentEvent.model_validate(json.loads(payload.data))


def nexus_remote_stream_source(endpoint: str) -> StreamSource:
    """Build the StreamSource for subagents fronted by ``endpoint``."""

    async def _events(
        client: Client, workflow_id: str, from_offset: int
    ) -> AsyncIterator[WorkflowStreamItem[AgentEvent]]:
        # workflow_id is really the subagent's task_id here — Nexus-routed instances never had
        # a real same-cluster workflow id.
        task_id = workflow_id
        nexus_client = client.create_nexus_client(service=SubagentService, endpoint=endpoint)
        cursor = from_offset
        while True:
            poll_handle = await nexus_client.start_operation(
                SubagentService.poll_task_updates,
                PollTaskUpdatesInput(
                    task_id=task_id, cursor=cursor, timeout_seconds=_POLL_TIMEOUT_SECONDS
                ),
            )
            polled = await poll_handle

            if polled.items:
                for item in polled.items:
                    event = _decode_stream_item(item)
                    yield WorkflowStreamItem(topic=TURN_EVENTS_TOPIC, data=event, offset=item.offset)
                cursor = polled.next_offset
            elif polled.closed:
                return  # same as a WorkflowStreamClient subscription ending (StopAsyncIteration)
            else:
                cursor = polled.next_offset
                await asyncio.sleep(_EMPTY_POLL_COOLDOWN.total_seconds())

    return _events
