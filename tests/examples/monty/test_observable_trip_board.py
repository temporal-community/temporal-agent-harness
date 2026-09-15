# ABOUTME: End-to-end test that the Monty trip board's OBSERVABLE state reaches a consumer
# intact. One Code Mode script drives the board through the moves a real turn makes — open a
# trip, book a flight, record it, tick a task off — and the assertions are the consumer's side
# of the bargain: one snapshot at registration, one patch per committed mutate() block, and a
# document that replays, op by op, into exactly what the agent holds.
#
# The replay is done with `jsonpatch` rather than the harness's own code on purpose. Nothing in
# temporal_agent_harness applies a patch, so a fold written here against the harness's helpers
# would only prove it agrees with itself; a third-party RFC 6902 implementation is what makes
# "any consumer can do this" a claim and not an assumption.
#
# Run with: uv run pytest tests/examples/monty/test_observable_trip_board.py -v

from __future__ import annotations

import copy
import uuid
from typing import Any

import jsonpatch
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
from temporal_agent_harness.plugin import AgentHarnessPlugin

from examples.monty import activities
from examples.monty.trip_board import DEFAULT_TASKS
from examples.monty.workflow import MontyDynamicAgentWorkflow

# One script, exercising every board tool a booking turn touches: open_trip (an `add` to a list),
# record_booking (a booking and the tasks it closes in ONE commit), and complete_trip_tasks (a
# `replace` on a nested field). The travel host calls are real activities, so the confirmation
# codes below are the ones the tools returned, never invented.
SCRIPT = (
    "import asyncio\n"
    "async def main():\n"
    '    trip = await open_trip({"description": "SFO to JFK, Jul 1-5", "traveler": "Ada Lovelace"})\n'
    '    tid = trip["trip_id"]\n'
    '    flights = await search_flights({"origin": "SFO", "destination": "JFK", "date": "2026-07-01"})\n'
    '    cheapest = min(flights["flights"], key=lambda f: f["price_usd"])\n'
    '    booked = await book_flight({"flight_id": cheapest["flight_id"], "passenger_name": "Ada Lovelace"})\n'
    "    await record_booking({\n"
    '        "trip_id": tid,\n'
    '        "kind": "flight",\n'
    '        "confirmation_code": booked["confirmation_code"],\n'
    '        "detail": cheapest["airline"] + " " + cheapest["flight_id"],\n'
    '        "price_usd": cheapest["price_usd"],\n'
    '        "completes": ["outbound flight"],\n'
    "    })\n"
    '    await complete_trip_tasks({"trip_id": "latest", "tasks": ["Confirm dates"]})\n'
    "    return await read_trip_board()\n"
    "asyncio.run(main())"
)


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"monty-board-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[MontyDynamicAgentWorkflow],
        plugins=[AgentHarnessPlugin(tools=activities.ALL_TOOLS)],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _run_script(client: Client, task_queue: str, script: str) -> list[AgentEvent]:
    """Run one script turn and return every turn_events envelope, in order."""
    handle = await client.start_workflow(
        MontyDynamicAgentWorkflow.run,
        AgentConfig(),
        id=f"MontyDynamicAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_script", payload={"script": script}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    events: list[AgentEvent] = []
    stream = WorkflowStreamClient.create(client, handle.id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        events.append(envelope)
        if envelope.event.type == AgentEventType.TURN_END:
            break
    return events


def _replay(events: list[AgentEvent]) -> dict[str, Any]:
    """Snapshot, then every patch in version order — the consumer's whole job."""
    snapshots = [e for e in events if e.event.type == AgentEventType.STATE_SNAPSHOT]
    patches = [e for e in events if e.event.type == AgentEventType.STATE_PATCH]
    assert len(snapshots) == 1, "a state is registered once, so it snapshots once"
    document = copy.deepcopy(snapshots[0].event.value)
    for patch in patches:
        # deepcopy of the ops too: jsonpatch is free to hold on to the objects it was
        # handed, and these came off the wire into events the other tests still read.
        document = jsonpatch.apply_patch(document, copy.deepcopy(patch.event.ops))
    return document


async def test_the_board_streams_and_replays_into_what_the_agent_holds(client_and_queue):
    client, task_queue = client_and_queue
    events = await _run_script(client, task_queue, SCRIPT)

    snapshot = next(e for e in events if e.event.type == AgentEventType.STATE_SNAPSHOT)
    assert snapshot.event.state_id == "trip_board"
    assert snapshot.event.version == 0
    assert snapshot.event.value == {"trips": []}
    # Registered in @workflow.init, before any turn exists — hence turn 0, the same
    # convention an operator command follows.
    assert snapshot.turn_number == 0

    patches = [e for e in events if e.event.type == AgentEventType.STATE_PATCH]
    assert len(patches) >= 3, "open_trip, record_booking and complete_trip_tasks each commit"
    assert [p.event.version for p in patches] == list(range(1, len(patches) + 1))
    # Every commit here came from the one script turn, so they all carry its number.
    assert {p.turn_number for p in patches} == {1}

    board = _replay(events)
    assert len(board["trips"]) == 1
    trip = board["trips"][0]
    assert trip["description"] == "SFO to JFK, Jul 1-5"
    assert trip["traveler"] == "Ada Lovelace"
    assert trip["status"] == "collecting"
    assert trip["hotels"] == []
    assert len(trip["flights"]) == 1
    assert trip["flights"][0]["confirmation_code"].startswith("AIR-")
    assert trip["flights"][0]["kind"] == "flight"

    # The checklist is the standard one, in order, with exactly the two items the script
    # closed ticked: "Confirm dates and traveler" by name via complete_trip_tasks, and
    # "Search and book the outbound flight" as a side effect of record_booking's
    # `completes`. Asserting the whole list rather than a count pins the ORDER too.
    assert [task["text"] for task in trip["remaining"]] == list(DEFAULT_TASKS)
    assert [task["done"] for task in trip["remaining"]] == [True, True, False, False, False]

    # The reply the model would have read back names the trip the board is holding.
    reply = next(e for e in events if e.event.type == AgentEventType.MESSAGE_HANDLER_END)
    assert trip["trip_id"] in reply.event.output["text"]


async def test_the_ops_address_exact_fields(client_and_queue):
    client, task_queue = client_and_queue
    events = await _run_script(client, task_queue, SCRIPT)
    patches = [e for e in events if e.event.type == AgentEventType.STATE_PATCH]
    paths = [op["path"] for patch in patches for op in patch.event.ops]

    # The point of the feature: no op re-sends the document or a whole collection. Ticking
    # one task is a `replace` on that task's `done`, not a re-send of `remaining` — so a
    # consumer can mark the field that moved instead of re-rendering the board.
    assert "" not in paths
    assert "/trips" not in paths

    assert any(path.startswith("/trips/0/remaining/") and path.endswith("/done") for path in paths)

    assert "/trips/-" in paths
    assert "/trips/0/flights/-" in paths


async def test_a_booking_and_the_task_it_closes_are_one_commit(client_and_queue):
    client, task_queue = client_and_queue
    events = await _run_script(client, task_queue, SCRIPT)
    patches = [e for e in events if e.event.type == AgentEventType.STATE_PATCH]

    # record_booking appends the booking and ticks what it finishes inside ONE mutate(),
    # so both land in a single patch. That is the guarantee worth a test of its own: a
    # consumer must never be able to observe a moment where the flight is on the board
    # and "book the outbound flight" is still outstanding, which is exactly what two
    # separate commits would give it.
    booking_patches = [
        p
        for p in patches
        if any(op["path"] == "/trips/0/flights/-" for op in p.event.ops)
    ]
    assert len(booking_patches) == 1
    ops = booking_patches[0].event.ops
    assert [op["op"] for op in ops] == ["add", "replace"]
    assert ops[1]["path"].startswith("/trips/0/remaining/")
    assert ops[1]["path"].endswith("/done")
    assert ops[1]["value"] is True
