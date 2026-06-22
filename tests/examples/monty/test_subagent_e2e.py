# ABOUTME: End-to-end test of the agents-as-subagents feature: a parent agent drives the
# barebones MontyDynamicAgent as a SUBAGENT. It uses a model-free stand-in parent
# (SubagentE2EParentWorkflow) whose handler calls the runner's start_subagent /
# run_subagent_turn / stop_subagent directly — so the whole subagent mechanism (the handle
# indirection, the run_subagent_turn activity against a real child, the per-subagent FIFO gate,
# and the turn-counter / stream-offset bookkeeping across turns) is exercised against a real
# child workflow with NO model (so no GEMINI_API_KEY needed; this is the Workstream D milestone
# assertion that the live conversational parent can't make in CI).
#
# Run with: uv run pytest tests/examples/monty/test_subagent_e2e.py -v

from __future__ import annotations

import re
import uuid

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness.agent_protocol import (
    AGENT_ID_LENGTH,
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.harness.subagent_activities import SubagentActivities

from temporal_agent_harness.harness.agent_client import AgentClient

from examples.monty import activities
from examples.monty.monty_activities import monty_resume_batch, monty_start_batch
from examples.monty.workflow import MontyDynamicAgentWorkflow
from ._subagent_e2e_parent import (
    ApprovalGatedSubagentParentWorkflow,
    SubagentE2EParentWorkflow,
)


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"subagent-e2e-{uuid.uuid4()}"
    # One worker hosts BOTH the parent and the child agent (the parent starts the child on this
    # same queue), the Monty batch + host activities the child needs, and the subagent-turn
    # activity the parent's runner dispatches — closed over the env client so it can talk to the
    # child workflow.
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[
            SubagentE2EParentWorkflow,
            ApprovalGatedSubagentParentWorkflow,
            MontyDynamicAgentWorkflow,
        ],
        activities=[
            *activities.ALL_ACTIVITIES,
            monty_start_batch,
            monty_resume_batch,
            SubagentActivities(env.client).run_subagent_turn,
        ],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _drive(
    client: Client, task_queue: str, scripts: list[str], *, concurrent: bool = False
) -> tuple[str, list[AgentEvent]]:
    """Start the parent, drive a subagent through ``scripts``, and return its reply text plus
    every event published on the PARENT's stream (so a caller can assert on the subagent
    lifecycle events too)."""
    handle = await client.start_workflow(
        SubagentE2EParentWorkflow.run,
        AgentConfig(),
        id=f"SubagentE2EParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type="drive",
            payload={
                "task_queue": task_queue,
                "scripts": scripts,
                "concurrent": concurrent,
            },
            expected_turn=1,
        ),
        result_type=AgentMessageReply,
    )

    stream = WorkflowStreamClient.create(client, handle.id)
    reply: str | None = None
    events: list[AgentEvent] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        events.append(envelope)
        if envelope.event.type == AgentEventType.REPLY:
            reply = envelope.event.output.get("text")
        if envelope.event.type == AgentEventType.TURN_END:
            break
    assert reply is not None, "parent turn ended without a reply"
    return reply, events


# A script with no host calls — fast + deterministic. MontyHostDriver renders the final value
# as ``result: <repr>`` (see _host_driver.run_script), so we assert on that.
def _const_script(value: int) -> str:
    return (
        "import asyncio\n"
        "async def main():\n"
        f"    return {value}\n"
        "asyncio.run(main())"
    )


async def test_parent_drives_subagent_across_sequential_turns(client_and_queue):
    client, task_queue = client_and_queue
    # Two scripts on ONE subagent instance, run as two sequential turns — exercises the turn
    # counter advancing and the stream-offset resume between turns (turn 2 streams from where
    # turn 1's turn_end left off).
    reply, events = await _drive(
        client, task_queue, [_const_script(42), _const_script(99)]
    )
    assert "result: 42" in reply
    assert "result: 99" in reply

    # The parent's stream carries the subagent lifecycle events (start then stop), each with the
    # real child workflow_id a UI would use to mount/unmount the child's own stream.
    started = [e.event for e in events if e.event.type == AgentEventType.SUBAGENT_STARTED]
    stopped = [e.event for e in events if e.event.type == AgentEventType.SUBAGENT_STOPPED]
    assert len(started) == 1 and len(stopped) == 1
    assert started[0].agent_key == "monty"
    assert started[0].workflow_id and started[0].workflow_id == stopped[0].workflow_id
    assert started[0].subagent_id == stopped[0].subagent_id

    # Each of the two scripts dispatches one message to the subagent, so the parent's stream
    # carries one SubagentMessageSent per turn — naming the same subagent/child and the target
    # handler, with the child turn numbers in order (1, then 2).
    messaged = [
        e.event for e in events if e.event.type == AgentEventType.SUBAGENT_MESSAGE_SENT
    ]
    assert len(messaged) == 2
    assert all(m.subagent_id == started[0].subagent_id for m in messaged)
    assert all(m.workflow_id == started[0].workflow_id for m in messaged)
    assert all(m.function == "run_script" for m in messaged)
    assert [m.subagent_turn for m in messaged] == [1, 2]


async def test_parent_drives_subagent_concurrent_gather(client_and_queue):
    client, task_queue = client_and_queue
    # Both scripts dispatched at once via asyncio.gather → the per-subagent FIFO gate serializes
    # them into two ordered turns against the one child. Both must complete.
    reply, events = await _drive(
        client,
        task_queue,
        [_const_script(7), _const_script(13)],
        concurrent=True,
    )
    assert "result: 7" in reply
    assert "result: 13" in reply

    # Even dispatched concurrently, the gate runs them as two ordered turns — so the two
    # SubagentMessageSent events must carry DISTINCT, ordered child turn numbers (1, then 2),
    # not both turn 1.
    messaged = [
        e.event for e in events if e.event.type == AgentEventType.SUBAGENT_MESSAGE_SENT
    ]
    assert sorted(m.subagent_turn for m in messaged) == [1, 2], [
        m.subagent_turn for m in messaged
    ]


async def test_subagent_runs_host_call_script(client_and_queue):
    client, task_queue = client_and_queue
    # A script that calls a durable host function, proving the child's full async batch / durable
    # activity stack works when driven across the subagent boundary (not just inline).
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    flights = await search_flights("SFO", "JFK", "2026-07-01")\n'
        '    cheapest = min(flights, key=lambda f: f["price_usd"])\n'
        '    booking = await book_flight(cheapest["flight_id"], "Ada Lovelace")\n'
        '    return await get_trip_summary([booking["confirmation_code"]])\n'
        "asyncio.run(main())"
    )
    reply, _ = await _drive(client, task_queue, [script])
    assert "Trip Itinerary" in reply
    assert "Passenger: Ada Lovelace" in reply
    assert "AIR-" in reply  # flight confirmation code prefix


async def _merged_send(
    client: Client, task_queue: str, scripts: list[str], *, stop: bool
) -> tuple[str, list[AgentEvent]]:
    """Drive the parent via ``AgentClient.send_message`` (the MERGED path) and collect every
    AgentEvent on the unified logical stream. ``stop`` controls whether the subagent is stopped
    at the end of the turn (a stopped subagent becomes a completed workflow — see the xfail note)."""
    handle = await client.start_workflow(
        SubagentE2EParentWorkflow.run,
        AgentConfig(),
        id=f"SubagentE2EParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    agent_client = AgentClient(client, handle.id)
    merged: list[AgentEvent] = []
    stream = await agent_client.send_message(
        "drive",
        {"task_queue": task_queue, "scripts": scripts, "stop": stop},
        expected_turn=1,
        on_item=lambda item, _seq: item,
        timeout=None,
    )
    async for item in stream:
        if isinstance(item, AgentEvent):
            merged.append(item)
    return handle.id, merged


def _assert_subagent_turns_nested_in_brackets(
    merged: list[AgentEvent], *, expected_child_turns: int
) -> None:
    """Assert the merged stream coalesced the parent + its live subagent into ONE stream with each
    child turn fully nested in its ``[subagent_message_sent … subagent_reply_received]`` bracket.

    The subagent's events carry ``agent_id == subagent_id`` — the short id the PARENT pushed down
    as the child's ``agent_id`` (the same value on the parent's ``SubagentStarted`` /
    ``SubagentMessageSent``) — so the merge is keyed off that short id, not the workflow_id."""
    subagent_ids = {
        e.event.subagent_id
        for e in merged
        if e.event.type == AgentEventType.SUBAGENT_STARTED
    }
    assert len(subagent_ids) == 1
    child_id = next(iter(subagent_ids))

    # Both agents' events appear on the ONE merged stream (stream isolation preserved — they came
    # from two independent workflow streams the merge coalesced), each self-identified by its short
    # agent_id. The child's events are exactly those stamped with the subagent_id.
    child_events = [e for e in merged if e.agent_id == child_id]
    parent_events = [e for e in merged if e.agent_id != child_id]
    assert parent_events, "no parent events on the merged stream"
    assert child_events, "the subagent's own events never appeared on the merged stream"

    # TREE-UNIQUENESS: the subagent stamps its OWN events with the COMPOUND id its parent assigned
    # — the parent's id plus one fresh 6-hex segment ("{parent}-{child}") — never the bare segment.
    # This is what lets a consumer group by agent_id across the whole tree without collisions; guard
    # it explicitly so a regression that drops the parent prefix on the child's stream is caught.
    parent_id = parent_events[0].agent_id
    assert all(e.agent_id == parent_id for e in parent_events)  # single (root) parent here
    assert re.fullmatch(rf"{re.escape(parent_id)}-[0-9a-f]{{{AGENT_ID_LENGTH}}}", child_id), (
        f"subagent agent_id {child_id!r} is not '<parent {parent_id}>-<{AGENT_ID_LENGTH}hex>' — "
        "the child dropped its parent prefix; tree-uniqueness is broken"
    )
    child_turn_ends = [
        e for e in child_events if e.event.type == AgentEventType.TURN_END
    ]
    assert len(child_turn_ends) == expected_child_turns, (
        f"expected {expected_child_turns} child turn_ends, got {len(child_turn_ends)}"
    )

    def _pos(pred) -> int:
        return next(i for i, e in enumerate(merged) if pred(e))

    for t in range(1, expected_child_turns + 1):
        open_i = _pos(
            lambda e, t=t: e.event.type == AgentEventType.SUBAGENT_MESSAGE_SENT
            and e.event.subagent_id == child_id
            and e.event.subagent_turn == t
        )
        close_i = _pos(
            lambda e, t=t: e.event.type == AgentEventType.SUBAGENT_REPLY_RECEIVED
            and e.event.subagent_id == child_id
            and e.event.subagent_turn == t
        )
        assert open_i < close_i
        positions = [
            i
            for i, e in enumerate(merged)
            if e.agent_id == child_id and e.turn_number == t
        ]
        assert positions, f"no child events for turn {t}"
        assert all(open_i < i < close_i for i in positions), (
            f"child turn {t} events escaped their bracket"
        )


async def test_merged_send_message_nests_live_subagent_events_in_brackets(client_and_queue):
    # The client-side stream-merge: AgentClient.send_message surfaces the CHILD's own turn events
    # (turn_started … reply … turn_end) INLINE on one logical stream, nested inside the parent's
    # [subagent_message_sent … subagent_reply_received] bracket — though parent and child publish
    # to independent workflow streams. End-to-end exercise of W1 (the new subagent_reply_received
    # publish + AgentEvent.agent_id) + W3 (the merge engine) against real workflows. The subagent
    # is left ALIVE (stop=False) so its stream is readable throughout (see the xfail twin below).
    client, task_queue = client_and_queue
    _parent_id, merged = await _merged_send(
        client, task_queue, [_const_script(42), _const_script(99)], stop=False
    )
    _assert_subagent_turns_nested_in_brackets(merged, expected_child_turns=2)


async def test_attach_after_stopped_subagent_degrades_gracefully(client_and_queue):
    # The REAL graceful-degradation path (distinct from the LIVE merge above, which reads the
    # child's detail in real time BEFORE the end-of-turn stop completes the child): here we drive
    # the subagent and STOP it, so by the time we REATTACH it is a COMPLETED workflow. workflow_streams
    # cannot read a completed workflow's stream, so the merge cannot mount the stopped child on
    # replay — it must DEGRADE rather than wedge: release the child's close gate so the parent renders
    # to its turn_end, and surface a non-fatal subagent_stream_unavailable marker for the child.
    client, task_queue = client_and_queue
    parent_id, _live = await _merged_send(
        client, task_queue, [_const_script(42), _const_script(99)], stop=True
    )

    # Reattach from the start: the parent's backlog still references the now-completed subagent.
    agent_client = AgentClient(client, parent_id)
    attached: list[AgentEvent] = []
    async for item in await agent_client.attach(
        on_item=lambda it, _o: it, from_offset=0, subagent_stall_grace_seconds=2.0
    ):
        if isinstance(item, AgentEvent):
            attached.append(item)

    # The subagent the parent drove (and stopped) — identified off the replayed subagent_started.
    child_ids = {
        e.event.subagent_id
        for e in attached
        if e.event.type == AgentEventType.SUBAGENT_STARTED
    }
    assert len(child_ids) == 1
    child_id = next(iter(child_ids))

    # The parent stream rendered FULLY despite the unreadable child — its turn_end is present (the
    # dead child's never-coming turn_end did not strand the parent's tail behind the close gate).
    parent_events = [e for e in attached if e.agent_id != child_id]
    assert parent_events[-1].event.type == AgentEventType.TURN_END

    # A non-fatal marker was surfaced for the stopped subagent (its own turn DETAIL is forgone), and
    # no actual child turn detail leaked onto the merged stream.
    markers = [
        e
        for e in attached
        if e.event.type == AgentEventType.SUBAGENT_STREAM_UNAVAILABLE
    ]
    assert markers and all(m.event.subagent_id == child_id for m in markers)
    child_detail = [
        e
        for e in attached
        if e.agent_id == child_id
        and e.event.type != AgentEventType.SUBAGENT_STREAM_UNAVAILABLE
    ]
    assert not child_detail


async def test_gated_concurrent_dispatches_get_distinct_turn_numbers(client_and_queue):
    # Reproduction of the real conversational agent's path: two sends dispatched concurrently
    # through the GENERATED tool under always_require_approvals, each gated on a real approval
    # BEFORE its body runs take_ticket. Even so, the two SubagentMessageSent events must carry
    # DISTINCT child turn numbers (1, then 2) — the bug report was both showing turn 1.
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        ApprovalGatedSubagentParentWorkflow.run,
        AgentConfig(),
        id=f"ApprovalGatedParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(
            type="drive",
            payload={
                "task_queue": task_queue,
                "scripts": [_const_script(7), _const_script(13)],
            },
            expected_turn=1,
        ),
        result_type=AgentMessageReply,
    )

    agent_client = AgentClient(client, handle.id)
    stream = WorkflowStreamClient.create(client, handle.id)
    approved: set[str] = set()
    messaged: list[AgentEvent] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=0, result_type=AgentEvent
    ):
        ev = item.data.event
        # Approve each gated send as soon as it asks — independent of arrival order.
        if ev.type == AgentEventType.TOOL_APPROVAL_REQUESTED and ev.tool_id not in approved:
            approved.add(ev.tool_id)
            await agent_client.approve_tool(ev.tool_id, approved=True)
        if ev.type == AgentEventType.SUBAGENT_MESSAGE_SENT:
            messaged.append(ev)
        if item.data.event.type == AgentEventType.TURN_END:
            break

    assert len(messaged) == 2
    # The payload (subagent) turn numbers must be distinct and ordered.
    assert sorted(m.subagent_turn for m in messaged) == [1, 2], [
        m.subagent_turn for m in messaged
    ]
