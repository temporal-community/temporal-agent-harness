"""The activity that reads what a target agent did: every tool call on its own event stream.

A tester holds the conversation (it sent the messages and got the replies), but not what the
agent did to produce them; that is only on the agent's stream. A judge needs it, because the
rubrics ask whether a reply rests on a real search or booking. So after the conversation, and
before the target is stopped, the tester reads the stream up to its current end, pairs each
tool's start with its end or error. Full evidence is preserved for grading and export.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStreamClient

from temporal_agent_harness.harness.agent_protocol import (
    TURN_EVENTS_TOPIC,
    AgentEvent,
    AgentEventType,
)

from examples.swarm_eval.models import ToolCall

COLLECT_TOOL_CALLS_ACTIVITY = "swarm_eval_collect_tool_calls"
COLLECT_TIMEOUT = timedelta(seconds=60)

class EvalActivities:
    """Activities that read other agents' streams, bound to the worker's client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn(name=COLLECT_TOOL_CALLS_ACTIVITY)
    async def collect_tool_calls(self, workflow_id: str) -> list[ToolCall]:
        """Every tool call ``workflow_id`` has made so far, in order, without truncating evidence."""
        stream = WorkflowStreamClient.create(self._client, workflow_id)
        end = await stream.get_offset()
        calls: dict[str, ToolCall] = {}
        if end == 0:
            return []
        # Offsets are global across topics. Reading only turn events can wait forever when
        # the captured end belongs to a state/status topic, so advance over every topic.
        async for item in stream.subscribe(from_offset=0):
            if item.offset >= end:
                break
            if item.topic == TURN_EVENTS_TOPIC:
                event = AgentEvent.model_validate(item.data).event
                if event.type == AgentEventType.TOOL_START:
                    calls[event.tool_id] = ToolCall(name=event.tool_name, input=str(event.tool_input))
                elif event.type == AgentEventType.TOOL_END and event.tool_id in calls:
                    calls[event.tool_id].output = event.tool_output
                elif event.type == AgentEventType.TOOL_ERROR and event.tool_id in calls:
                    calls[event.tool_id].error = event.message
            if item.offset + 1 >= end:
                break
        ordered = list(calls.values())
        return ordered
