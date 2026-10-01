# ABOUTME: Code Mode under an approval policy, with no model in the loop. A refused host call
# must reach the script as PermissionError (catchable, with the reason) without abandoning the
# rest of its batch, and the on_host_call_result observer must see exactly the calls that ran,
# under the same tool ids their lifecycle events carry.

from __future__ import annotations

import json
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
from temporal_agent_harness.plugin import AgentHarnessPlugin

from tests.harness._code_mode_policy_parent import (
    POLICY_HOOK_TOOLS,
    REFUSAL_REASON,
    CodeModePolicyParentWorkflow,
)


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"code-mode-policy-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CodeModePolicyParentWorkflow],
        plugins=[AgentHarnessPlugin(tools=POLICY_HOOK_TOOLS)],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _run(client: Client, task_queue: str, script: str) -> tuple[dict, list[AgentEvent]]:
    handle = await client.start_workflow(
        CodeModePolicyParentWorkflow.run,
        AgentConfig(),
        id=f"CodeModePolicyParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_code", payload={"script": script}),
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
        if envelope.event.type == AgentEventType.MESSAGE_HANDLER_END:
            reply = envelope.event.output.get("text")
        if envelope.event.type == AgentEventType.TURN_END:
            break
    assert reply is not None, "turn ended without a reply"
    return json.loads(reply), events


def _tool_starts(events: list[AgentEvent], tool_name: str) -> list[AgentEvent]:
    return [
        e
        for e in events
        if e.event.type == AgentEventType.TOOL_START and e.event.tool_name == tool_name
    ]


async def test_refused_call_raises_permission_error_the_script_can_catch(client_and_queue):
    client, task_queue = client_and_queue
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    total = (await add({"a": 1, "b": 2}))["total"]\n'
        "    try:\n"
        '        await greet({"name": "Mallory"})\n'
        '        refused = "not refused"\n'
        "    except PermissionError as e:\n"
        "        refused = str(e)\n"
        '    hello = (await greet({"name": "Ada"}))["message"]\n'
        "    return [total, refused, hello]\n"
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    output = reply["output"]
    # The script kept going past the refusal, and saw the evaluator's reason.
    assert "result: [3," in output
    assert REFUSAL_REASON in output
    assert "hi Ada" in output
    # The refused call never ran; the approved ones did.
    assert [e.event.tool_input for e in _tool_starts(events, "greet")] == [
        {"request": {"name": "Ada"}}
    ]


async def test_refusal_in_a_gathered_coroutine_is_caught_there(client_and_queue):
    client, task_queue = client_and_queue
    script = (
        "import asyncio\n"
        "async def greet_safely(name: str) -> str:\n"
        "    try:\n"
        '        return (await greet({"name": name}))["message"]\n'
        "    except PermissionError as e:\n"
        '        return "refused: " + str(e)\n'
        "async def main():\n"
        '    return await asyncio.gather(*[greet_safely(n) for n in ["Ada", "Mallory", "Bo"]])\n'
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    output = reply["output"]
    assert "Script error" not in output
    assert "'hi Ada'" in output and "'hi Bo'" in output
    assert f"refused: tool 'greet' was not approved: {REFUSAL_REASON}" in output
    assert sorted(e.event.tool_input["request"]["name"] for e in _tool_starts(events, "greet")) == [
        "Ada",
        "Bo",
    ]


async def test_refusal_does_not_abandon_its_batch(client_and_queue):
    client, task_queue = client_and_queue
    # Uncaught, the refusal fails the script — but only after every sibling in the gathered
    # batch has run to its own end, so nothing is left in flight behind the failed script.
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    a, g = await asyncio.gather(add({"a": 2, "b": 5}), greet({"name": "Mallory"}))\n'
        "    return [a, g]\n"
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    assert "PermissionError" in reply["output"]
    assert REFUSAL_REASON in reply["output"]
    assert len(_tool_starts(events, "add")) == 1
    assert any(
        e.event.type == AgentEventType.TOOL_END and e.event.tool_name == "add" for e in events
    )
    assert not _tool_starts(events, "greet")
    assert [r["tool_name"] for r in reply["observed"]] == ["add"]


async def test_observer_sees_each_completed_call_under_its_tool_id(client_and_queue):
    client, task_queue = client_and_queue
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    r = await add({"a": 3, "b": 4})\n'
        '    g = await greet({"name": "Ada"})\n'
        "    return [r, g]\n"
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    observed = reply["observed"]
    assert [(r["tool_name"], r["tool_input"], r["result"]) for r in observed] == [
        ("add", {"request": {"a": 3, "b": 4}}, {"total": 7}),
        ("greet", {"request": {"name": "Ada"}}, {"message": "hi Ada"}),
    ]
    started = {e.event.tool_name: e.event.tool_id for e in events if e.event.type == AgentEventType.TOOL_START}
    assert observed[0]["tool_id"] == started["add"]
    assert observed[1]["tool_id"] == started["greet"]
