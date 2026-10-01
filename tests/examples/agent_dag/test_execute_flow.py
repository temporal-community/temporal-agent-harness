# ABOUTME: End-to-end test of DagBuilderAgent's `execute` handler with no model in the loop. A
# flow script runs in Code Mode over `run_agent`, whose calls start real subagents: an echo
# stand-in for DagStepAgent that replies with the spec the script gave it. Covers the script's
# spec reaching each step as init data, parallel then sequential steps, every step being
# stopped, and a bad tool name failing the type check before any step starts.
#
# Run with: uv run --group examples pytest tests/examples/agent_dag -v

from __future__ import annotations

import uuid

import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.agent_dag.workflow import DagBuilderAgentWorkflow
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin

from ._echo_step_agent import EchoStepAgentWorkflow

FLOW = '''\
"""Two scouts in parallel -> planner."""
import asyncio

async def main():
    flights, weather = await asyncio.gather(
        run_agent({"name": "Flights", "instructions": "Find flights.", "prompt": "SFO to SEA",
                   "tools": ["search_flights"]}),
        run_agent({"name": "Weather", "instructions": "Check weather.", "prompt": "Seattle",
                   "tools": ["get_weather"], "model": "gpt-6.1-sol"}),
    )
    plan = await run_agent({
        "name": "Planner",
        "instructions": "Plan the trip.",
        "prompt": f"{flights['output']} / {weather['output']}",
    })
    return plan["output"]

asyncio.run(main())
'''


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"agent-dag-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[DagBuilderAgentWorkflow, EchoStepAgentWorkflow],
        plugins=[AgentHarnessPlugin()],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _events(client: Client, workflow_id: str) -> list[AgentEvent]:
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        events.append(item.data)
        if item.data.event.type == AgentEventType.TURN_END:
            break
    return events


async def _execute(client: Client, task_queue: str, script: str) -> tuple[str, list[AgentEvent]]:
    handle = await client.start_workflow(
        DagBuilderAgentWorkflow.run,
        AgentConfig(),
        id=f"DagBuilder-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="execute", payload={"script": script}),
        result_type=AgentMessageReply,
    )
    events = await _events(client, handle.id)
    [end] = [e.event for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END]
    return end.output["text"], events


async def test_flow_runs_parallel_then_sequential_steps(client_and_queue):
    client, task_queue = client_and_queue
    reply, events = await _execute(client, task_queue, FLOW)

    # Each step echoed the spec the script gave it, and the planner saw both scouts' replies.
    assert (
        "Planner[|gpt-6-luna]: Flights[search_flights|gpt-6-luna]: SFO to SEA"
        " / Weather[get_weather|gpt-6.1-sol]: Seattle"
    ) in reply

    started = [e.event for e in events if e.event.type == AgentEventType.SUBAGENT_STARTED]
    stopped = [e.event for e in events if e.event.type == AgentEventType.SUBAGENT_STOPPED]
    assert len(started) == 3
    assert {s.subagent_id for s in started} == {s.subagent_id for s in stopped}

    # The planner is started only once both scouts have replied.
    order = [
        (e.event.type, getattr(e.event, "subagent_id", None))
        for e in events
        if e.event.type
        in (AgentEventType.SUBAGENT_STARTED, AgentEventType.SUBAGENT_REPLY_RECEIVED)
    ]
    assert [t for t, _ in order] == [
        AgentEventType.SUBAGENT_STARTED,
        AgentEventType.SUBAGENT_STARTED,
        AgentEventType.SUBAGENT_REPLY_RECEIVED,
        AgentEventType.SUBAGENT_REPLY_RECEIVED,
        AgentEventType.SUBAGENT_STARTED,
        AgentEventType.SUBAGENT_REPLY_RECEIVED,
    ]


# Building a step by calling its type, as in Python, gives the step agent the same plain dict.
TYPED_FLOW = FLOW.replace(
    'run_agent({"name": "Weather", "instructions": "Check weather.", "prompt": "Seattle",\n'
    '                   "tools": ["get_weather"], "model": "gpt-6.1-sol"})',
    'run_agent(AgentStep(name="Weather", instructions="Check weather.", prompt="Seattle",\n'
    '                            tools=["get_weather"], model="gpt-6.1-sol"))',
)


async def test_a_flow_can_build_its_steps_by_calling_their_type(client_and_queue):
    assert TYPED_FLOW != FLOW
    client, task_queue = client_and_queue
    reply, _ = await _execute(client, task_queue, TYPED_FLOW)

    assert (
        "Planner[|gpt-6-luna]: Flights[search_flights|gpt-6-luna]: SFO to SEA"
        " / Weather[get_weather|gpt-6.1-sol]: Seattle"
    ) in reply


async def test_importing_the_host_stubs_fails_before_any_step(client_and_queue):
    # The host stubs are declarations for the type checker, not a module the script can import,
    # so the import is rejected when the script is checked, before any host call.
    client, task_queue = client_and_queue
    script = "from type_stubs import AgentStep, AgentStepResult, run_agent\n" + FLOW
    reply, events = await _execute(client, task_queue, script)

    assert reply.startswith("Script error (MontyTypingError")
    assert "Cannot resolve imported module `type_stubs`" in reply
    assert not [e for e in events if e.event.type == AgentEventType.SUBAGENT_STARTED]


async def test_unknown_tool_fails_type_check_before_any_step(client_and_queue):
    client, task_queue = client_and_queue
    script = FLOW.replace('"search_flights"', '"launch_rockets"')
    reply, events = await _execute(client, task_queue, script)

    assert "launch_rockets" in reply
    assert not [e for e in events if e.event.type == AgentEventType.SUBAGENT_STARTED]
