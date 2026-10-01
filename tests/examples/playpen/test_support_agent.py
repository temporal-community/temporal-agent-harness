# ABOUTME: End-to-end test of the Playpen support agent with no model in the loop: the demo's
# two scripted beats run through the real workflow, Code Mode sandbox, store activities and
# Dogwood CLI, and the test plays the person who approves the escalated refund. Asserts what the
# store actually did, not just what the agent said. Needs DOGWOOD_CLI (skipped otherwise).

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from examples.playpen import store
from examples.playpen.workflow import PlaypenSupportAgentWorkflow
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TOOL_APPROVAL_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    AutoApprovalVerdict,
    ToolApprovalDecision,
    ToolApprovalResult,
)
from temporal_agent_harness.playpen.activity import DogwoodPolicyStore
from temporal_agent_harness.plugin import AgentHarnessPlugin
from tests.playpen.conftest import POLICY_STORE, dogwood_cli  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def fresh_store():
    store.REFUNDS.clear()
    store.OUTBOX.clear()
    yield


@pytest_asyncio.fixture
async def client_and_queue(dogwood_cli):  # noqa: F811
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"playpen-test-{uuid.uuid4()}"
    policy_store = DogwoodPolicyStore(POLICY_STORE, dogwood_cli=dogwood_cli)
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[PlaypenSupportAgentWorkflow],
        activities=[policy_store.decide],
        plugins=[AgentHarnessPlugin(tools=store.STORE_TOOLS)],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _start(client: Client, task_queue: str) -> WorkflowHandle:
    return await client.start_workflow(
        PlaypenSupportAgentWorkflow.run,
        AgentConfig(),
        id=f"PlaypenSupportAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )


async def _run_beat(
    client: Client, handle: WorkflowHandle, beat: str, *, approve: bool = True
) -> tuple[str, list[AgentEvent]]:
    """Run one scripted beat; answer every escalation with ``approve``; return the reply."""
    accepted = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_scripted_beat", payload={"beat": beat}),
        result_type=AgentMessageReply,
    )
    stream = WorkflowStreamClient.create(client, handle.id)
    reply: str | None = None
    events: list[AgentEvent] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC], from_offset=accepted.accepted_offset, result_type=AgentEvent
    ):
        envelope: AgentEvent = item.data
        event = envelope.event
        events.append(envelope)
        if (
            event.type == AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED
            and event.verdict == AutoApprovalVerdict.ESCALATE
        ):
            await handle.execute_update(
                TOOL_APPROVAL_UPDATE,
                ToolApprovalDecision(
                    tool_id=event.tool_id, approved=approve, reason="checked by a person"
                ),
                result_type=ToolApprovalResult,
            )
        if event.type == AgentEventType.MESSAGE_HANDLER_END:
            reply = event.output.get("text")
        if event.type == AgentEventType.TURN_END:
            break
    assert reply is not None, "turn ended without a reply"
    # The reply shows the script and then its result; assert on the result alone, since the
    # script's own source mentions the outcomes it handles.
    assert "Result:" in reply, reply
    return reply.split("Result:", 1)[1], events


def _evaluations(events: list[AgentEvent]) -> list[tuple[str, str, list[str]]]:
    return [
        (e.event.tool_name, e.event.verdict.value, e.event.details["rules"])
        for e in events
        if e.event.type == AgentEventType.AUTO_APPROVAL_EVALUATION_ENDED
    ]


async def test_mail_goes_only_to_the_address_on_file(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    reply, events = await _run_beat(client, handle, "ticket_t1")

    assert [e.to for e in store.OUTBOX] == ["alice.chen@example.com"]
    assert [(r.order_id, r.amount_cents) for r in store.REFUNDS] == [("o-5001", 3400)]
    assert "REFUSED" in reply and "address on file" in reply
    assert ("send_email", "deny", []) in _evaluations(events)
    assert ("send_email", "approve", ["email_address_on_file"]) in _evaluations(events)


async def test_the_queue_refunds_each_order_once_and_asks_about_the_big_one(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    reply, events = await _run_beat(client, handle, "work_queue")

    # T-1 and T-3 are the same order, refunded concurrently in one gathered batch: one of them
    # gets through, the other is refused. T-4 is over $50 and ran once a person approved it.
    assert sorted((r.order_id, r.amount_cents) for r in store.REFUNDS) == [
        ("o-5001", 3400),
        ("o-5002", 4800),
        ("o-5003", 6200),
    ]
    refunds = [v for v in _evaluations(events) if v[0] == "issue_refund"]
    assert sorted(v[1] for v in refunds) == ["approve", "approve", "deny", "escalate"]
    assert ("issue_refund", "deny", ["one_refund_per_order"]) in refunds
    assert ("issue_refund", "escalate", ["large_refund_needs_a_person"]) in refunds
    assert reply.count("REFUSED") == 1 and "already been refunded" in reply
    assert "Script error" not in reply


async def test_a_person_can_turn_the_big_refund_down(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    reply, _ = await _run_beat(client, handle, "work_queue", approve=False)

    assert "o-5003" not in {r.order_id for r in store.REFUNDS}
    assert "T-4: REFUSED" in reply and "checked by a person" in reply


async def test_policy_holds_across_turns(client_and_queue):
    client, task_queue = client_and_queue
    handle = await _start(client, task_queue)
    await _run_beat(client, handle, "ticket_t1")
    reply, _ = await _run_beat(client, handle, "work_queue")

    # Beat 1 already refunded o-5001, so this session refuses both T-1 and T-3.
    assert [r.order_id for r in store.REFUNDS].count("o-5001") == 1
    assert "T-1: REFUSED" in reply and "T-3: REFUSED" in reply
