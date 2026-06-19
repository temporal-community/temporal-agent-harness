# ABOUTME: End-to-end test for the Monty dynamic agent against the time-skipping test
# server. Sends a RunScript whose script calls BOTH host functions (each backed by a
# durable activity) and asserts the reply carries the script's stdout + final value.
#
# This doubles as the experiment's key check: it runs under the DEFAULT (sandboxed)
# workflow runner, so a pass means the pydantic-monty interpreter both imports through
# the Temporal sandbox AND drives external async host functions across activity awaits.
#
# Run with: uv run pytest tests/examples/monty/test_workflow.py -v

from __future__ import annotations

import uuid

import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)

from examples.monty import activities
from examples.monty.monty_activities import monty_resume_batch, monty_start_batch
from examples.monty.workflow import MontyDynamicAgentWorkflow


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"monty-agent-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[MontyDynamicAgentWorkflow],
        activities=[*activities.ALL_ACTIVITIES, monty_start_batch, monty_resume_batch],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _reply_text(client: Client, workflow_id: str) -> str:
    stream = WorkflowStreamClient.create(client, workflow_id)
    reply: str | None = None
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
    ):
        envelope: AgentEvent = item.data
        if envelope.event.type == AgentEventType.REPLY:
            # run_script returns a TextReply; read its text off the output dict.
            reply = envelope.event.output.get("text")
        if envelope.event.type == AgentEventType.TURN_END:
            break
    assert reply is not None, "turn ended without a reply"
    return reply


async def test_script_calls_host_functions(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        MontyDynamicAgentWorkflow.run,
        AgentConfig(),
        id=f"MontyDynamicAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )

    # Concurrently search flights AND hotels (one gathered batch → two durable activities
    # in flight at once), then book the cheapest flight, then summarize — exercising the
    # async batch driver end-to-end: a multi-call concurrent batch followed by dependent
    # sequential calls. Host functions are ASYNC and must be awaited.
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    flights, hotels = await asyncio.gather(\n'
        '        search_flights("SFO", "JFK", "2026-07-01"),\n'
        '        search_hotels("New York", "2026-07-01", "2026-07-05"),\n'
        "    )\n"
        '    cheapest = min(flights, key=lambda f: f["price_usd"])\n'
        '    flight = await book_flight(cheapest["flight_id"], "Ada Lovelace")\n'
        "    print(f\"airline={cheapest['airline']} hotels={len(hotels)}\")\n"
        '    return await get_trip_summary([flight["confirmation_code"]])\n'
        "asyncio.run(main())"
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_script", payload={"script": script}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    reply = await _reply_text(client, handle.id)
    # The reply carries the printed airline line plus the itinerary (final expression).
    assert "airline=" in reply
    assert "Trip Itinerary" in reply
    assert "Passenger: Ada Lovelace" in reply
    assert "AIR-" in reply  # flight confirmation code prefix


async def test_script_syntax_error_is_reported(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        MontyDynamicAgentWorkflow.run,
        AgentConfig(),
        id=f"MontyDynamicAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_script", payload={"script": "def ("}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    reply = await _reply_text(client, handle.id)
    assert "Script error" in reply
