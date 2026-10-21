# ABOUTME: The conformance driver: plays scenarios against a worker hosting the scripted agent,
# as a client would (sending messages, approving tools, answering callbacks when given events
# arrive), records each agent's turn_events stream, and normalizes the recording into the form
# conformance/goldens/ holds.

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_client import AgentClient, MidTurnRejectedError
from temporal_agent_harness.harness.agent_protocol import (
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    MessageDisposition,
)
from temporal_agent_harness.harness.subagent_activities import SubagentActivities
from tests.conformance.script import Handler, Script
from tests.conformance.scripted_agent import ScriptedAgent, add

TIMEOUT = timedelta(seconds=30)


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Send(_Action):
    """Send a message to one of the scripted agent's handlers."""

    action: Literal["send"] = "send"
    handler: Handler
    script: Script


class Await(_Action):
    """Wait until the root agent, or one of its subagents, publishes a matching event."""

    action: Literal["await"] = "await"
    subagent: int | None = Field(
        default=None, description="Match on this subagent's stream (1 is the first started)."
    )
    event: AgentEventType
    tool_id: str | None = None
    turn_number: int | None = None
    send: int | None = Field(
        default=None, description="Match only events of this send's message (1 is the first)."
    )


class Approve(_Action):
    action: Literal["approve"] = "approve"
    tool_id: str
    approved: bool
    reason: str | None = None


class AnswerCallback(_Action):
    """Fulfill a pending callback with ``result``, or fail it with ``error``."""

    action: Literal["answer_callback"] = "answer_callback"
    tool_id: str
    result: str | None = None
    error: str | None = None


Action = Annotated[Send | Await | Approve | AnswerCallback, Field(discriminator="action")]


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    actions: list[Action]


class SendResult(BaseModel):
    """What one send returned: the acceptance, or the error that refused it."""

    send: int
    turn_number: int | None = None
    turn_id: str | None = None
    message_id: str | None = None
    disposition: MessageDisposition | None = None
    error: str | None = None


@dataclass
class Recording:
    """One scenario's raw output: every agent's stream, root first, and each send's result."""

    streams: list[list[AgentEvent]]
    sends: list[SendResult]


def load_scenario(path: Path) -> Scenario:
    return Scenario.model_validate_json(path.read_text())


@asynccontextmanager
async def scripted_worker() -> AsyncIterator[tuple[Client, str]]:
    """A local Temporal server and a worker hosting the scripted agent."""
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter,
        dev_server_existing_path=os.environ.get("TEMPORAL_CLI_PATH"),
    )
    try:
        task_queue = f"conformance-{uuid.uuid4()}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ScriptedAgent],
            activities=[agent.tool_activity(add), SubagentActivities(env.client).run_subagent_turn],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            yield env.client, task_queue
    finally:
        await env.shutdown()


@dataclass
class _StreamReader:
    """Collects one agent's stream in the background and wakes waiters on each event.

    Starts a reader for each subagent as soon as the subagent starts: a stopped subagent's stream
    cannot be read afterwards."""

    client: Client
    workflow_id: str
    events: list[AgentEvent] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    children: dict[str, _StreamReader] = field(default_factory=dict)
    _changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    _task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        stream = WorkflowStreamClient.create(self.client, self.workflow_id)
        async for item in stream.subscribe(
            topics=[TURN_EVENTS_TOPIC],
            from_offset=0,
            result_type=AgentEvent,
            poll_cooldown=timedelta(milliseconds=10),
        ):
            if item.data.event.type == AgentEventType.SUBAGENT_STARTED:
                child = _StreamReader(self.client, item.data.event.workflow_id)
                child.start()
                self.children[child.workflow_id] = child
            async with self._changed:
                self.events.append(item.data)
                self.offsets.append(item.offset)
                self._changed.notify_all()

    async def wait_until(self, done: Callable[[list[AgentEvent]], bool]) -> None:
        async with asyncio.timeout(TIMEOUT.total_seconds()):
            async with self._changed:
                await self._changed.wait_for(lambda: done(self.events))

    async def wait_through_offset(self, offset: int) -> None:
        async with asyncio.timeout(TIMEOUT.total_seconds()):
            async with self._changed:
                await self._changed.wait_for(lambda: bool(self.offsets) and self.offsets[-1] >= offset)

    async def child(self, number: int) -> _StreamReader:
        """The reader of the ``number``th subagent to start, once it has started."""
        await self.wait_until(lambda _: len(self.children) >= number)
        return list(self.children.values())[number - 1]

    async def stop(self) -> None:
        for child in self.children.values():
            await child.stop()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


async def record(client: Client, task_queue: str, scenario: Scenario) -> Recording:
    """Play ``scenario`` against a new scripted agent and record what it publishes."""
    workflow_id = f"conformance-{uuid.uuid4()}"
    await client.start_workflow(
        ScriptedAgent.run, AgentConfig(), id=workflow_id, task_queue=task_queue
    )
    agent_client = AgentClient(client, workflow_id)
    root = _StreamReader(client, workflow_id)
    root.start()
    sends: list[SendResult] = []
    message_ids: dict[int, str] = {}
    try:
        for action in scenario.actions:
            match action:
                case Send():
                    number = len(sends) + 1
                    try:
                        reply = await agent_client.submit_message(
                            action.handler, action.script.model_dump(mode="json")
                        )
                    except MidTurnRejectedError:
                        sends.append(SendResult(send=number, error="MidTurnRejected"))
                    else:
                        message_ids[number] = reply.message_id
                        sends.append(
                            SendResult(
                                send=number,
                                turn_number=reply.turn_number,
                                turn_id=reply.turn_id,
                                message_id=reply.message_id,
                                disposition=reply.disposition,
                            )
                        )
                case Await():
                    matches = _matcher(action, message_ids)
                    reader = root if action.subagent is None else await root.child(action.subagent)
                    await reader.wait_until(lambda events: any(map(matches, events)))
                case Approve():
                    await agent_client.approve_tool(
                        action.tool_id, approved=action.approved, reason=action.reason
                    )
                case AnswerCallback():
                    await agent_client.provide_callback_result(
                        action.tool_id, result=action.result, error=action.error
                    )
        await _wait_until_idle(agent_client, client, workflow_id, root)
        streams = [list(root.events)]
        for child_id, turns in _turns_sent_to_subagents(root.events).items():
            child = root.children[child_id]
            await child.wait_until(lambda events: _turn_ends(events) >= turns)
            streams.append(_through_nth_turn_end(child.events, turns))
    finally:
        await root.stop()
    return Recording(streams=streams, sends=sends)


def _matcher(action: Await, message_ids: dict[int, str]) -> Callable[[AgentEvent], bool]:
    message_id = message_ids[action.send] if action.send is not None else None

    def matches(event: AgentEvent) -> bool:
        return (
            event.event.type == action.event
            and (action.tool_id is None or getattr(event.event, "tool_id", None) == action.tool_id)
            and (action.turn_number is None or event.turn_number == action.turn_number)
            and (message_id is None or event.message_id == message_id)
        )

    return matches


async def _wait_until_idle(
    agent_client: AgentClient, client: Client, workflow_id: str, root: _StreamReader
) -> None:
    async with asyncio.timeout(TIMEOUT.total_seconds()):
        while True:
            status = await agent_client.get_status()
            if not status.turn_active and not status.pending_turns:
                break
            await asyncio.sleep(0.05)
    head = await WorkflowStreamClient.create(client, workflow_id).get_offset()
    await root.wait_through_offset(head - 1)


def _turns_sent_to_subagents(events: list[AgentEvent]) -> dict[str, int]:
    """Each subagent's workflow id, in start order, with the number of turns sent to it."""
    sent: dict[str, int] = {}
    for event in events:
        if event.event.type == AgentEventType.SUBAGENT_STARTED:
            sent[event.event.workflow_id] = 0
        elif event.event.type == AgentEventType.SUBAGENT_MESSAGE_SENT:
            sent[event.event.workflow_id] += 1
    return sent


def _turn_ends(events: list[AgentEvent]) -> int:
    return sum(1 for event in events if event.event.type == AgentEventType.TURN_END)


def _through_nth_turn_end(events: list[AgentEvent], n: int) -> list[AgentEvent]:
    ends = 0
    for i, event in enumerate(events):
        if event.event.type == AgentEventType.TURN_END:
            ends += 1
            if ends == n:
                return events[: i + 1]
    return list(events)


# -- Normalization -------------------------------------------------------------------------

_ID_FIELDS = {
    "agent_id": "agent",
    "subagent_id": "agent",
    "turn_id": "turn",
    "message_id": "message",
    "workflow_id": "workflow",
    "evaluation_id": "evaluation",
}


class _Ids:
    """Replaces each minted id with ``<kind>-<n>``, numbered by first appearance.

    An empty id is not minted: it is what events published outside a turn carry as ``turn_id``."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def name(self, kind: str, value: str) -> str:
        if value not in self._names:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._names[value] = f"{kind}-{self._counts[kind]}"
        return self._names[value]

    def replace(self, obj: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            key: self.name(_ID_FIELDS[key], value)
            if key in _ID_FIELDS and isinstance(value, str) and value
            else value
            for key, value in obj.items()
        }


@dataclass
class Normalized:
    events: list[dict[str, JsonValue]]
    sends: list[dict[str, JsonValue]]

    def events_jsonl(self) -> str:
        return "".join(json.dumps(e, sort_keys=True) + "\n" for e in self.events)

    def sends_jsonl(self) -> str:
        return "".join(json.dumps(s, sort_keys=True) + "\n" for s in self.sends)


def normalize(recording: Recording) -> Normalized:
    """Ids by first appearance (streams root first, then sends), timestamps dropped."""
    ids = _Ids()
    events: list[dict[str, JsonValue]] = []
    for stream in recording.streams:
        for event in stream:
            envelope = ids.replace(event.model_dump(mode="json", exclude={"timestamp", "event"}))
            envelope["event"] = ids.replace(event.event.model_dump(mode="json"))
            events.append(envelope)
    sends = [ids.replace(s.model_dump(mode="json", exclude_none=True)) for s in recording.sends]
    return Normalized(events=events, sends=sends)


def timestamps_never_decrease(recording: Recording) -> bool:
    return all(
        earlier.timestamp <= later.timestamp
        for stream in recording.streams
        for earlier, later in zip(stream, stream[1:])
    )
