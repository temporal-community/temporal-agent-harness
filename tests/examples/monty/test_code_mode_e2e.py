# ABOUTME: End-to-end test of the Code Mode harness feature with NO model in the loop. A
# model-free parent (CodeModeE2EParentWorkflow) builds a code_mode_tool over two deterministic
# activity tools and runs scripts through it, so the full stack — stub generation, the sandbox
# batch loop, host-call dispatch via run_tool (coercion + result marshalling + tool lifecycle),
# and pre-run type checking — is exercised against real activities under a WorkflowEnvironment.
#
# Run with: uv run pytest tests/examples/monty/test_code_mode_e2e.py -v

from __future__ import annotations

import uuid

import pytest_asyncio
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.harness.code_mode.activities import CODE_MODE_ACTIVITIES

from ._code_mode_e2e_parent import CODE_MODE_TOOLS, CodeModeE2EParentWorkflow


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    task_queue = f"code-mode-e2e-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[CodeModeE2EParentWorkflow],
        # The generic Code Mode stepping activities + the durable bodies of the host tools
        # (registered the normal way, like any @agent.activity_tool_defn).
        activities=[
            *CODE_MODE_ACTIVITIES,
            *(agent.tool_activity(t) for t in CODE_MODE_TOOLS),
        ],
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _run(
    client: Client, task_queue: str, script: str
) -> tuple[str, list[AgentEvent]]:
    """Start the parent, run one script through Code Mode, and return the reply text plus every
    event published on the parent's stream (so callers can assert on the tool lifecycle too)."""
    handle = await client.start_workflow(
        CodeModeE2EParentWorkflow.run,
        AgentConfig(),
        id=f"CodeModeE2EParent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="run_code", payload={"script": script}, expected_turn=1),
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
    assert reply is not None, "turn ended without a reply"
    return reply, events


def _tool_starts(events: list[AgentEvent], tool_name: str) -> list[AgentEvent]:
    return [
        e
        for e in events
        if e.event.type == AgentEventType.TOOL_START and e.event.tool_name == tool_name
    ]


async def test_script_runs_host_calls_and_returns_result(client_and_queue):
    client, task_queue = client_and_queue
    # Faithful (non-flattened) host signatures: pass the request as a dict, read the result dict.
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    r = await add({"a": 3, "b": 4})\n'
        '    g = await greet({"name": "Ada"})\n'
        "    return f\"{g['message']} total={r['total']}\"\n"
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    assert "hi Ada total=7" in reply

    # Each host call ran through run_tool, so it published its own tool_start/tool_end lifecycle.
    assert len(_tool_starts(events, "add")) == 1
    assert len(_tool_starts(events, "greet")) == 1
    assert any(
        e.event.type == AgentEventType.TOOL_END and e.event.tool_name == "add"
        for e in events
    )


async def test_gathered_calls_run_as_one_concurrent_batch(client_and_queue):
    client, task_queue = client_and_queue
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    a, b = await asyncio.gather(add({"a": 1, "b": 2}), add({"a": 3, "b": 4}))\n'
        '    return a["total"] + b["total"]\n'
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    assert "result: 10" in reply
    # Both add calls in the one gathered batch executed (two tool_start events for add).
    assert len(_tool_starts(events, "add")) == 2


async def test_injected_params_are_supplied_by_the_harness(client_and_queue):
    client, task_queue = client_and_queue
    # `echo`'s host signature is echo(label) — the Injected `secret` is hidden from the script and
    # supplied by the harness from the code_mode_tool `injections`.
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    r = await echo("hello")\n'
        '    return r["value"]\n'
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    assert "result: 'hello:s3cr3t'" in reply
    assert len(_tool_starts(events, "echo")) == 1


async def test_type_error_is_reported_and_no_host_call_runs(client_and_queue):
    client, task_queue = client_and_queue
    # "x" is a str where add expects int — rejected by the generated stubs BEFORE running.
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    return await add({"a": "x", "b": 4})\n'
        "asyncio.run(main())"
    )
    reply, events = await _run(client, task_queue, script)
    assert "Script error" in reply
    # Pre-run type checking gates the bad script, so no host activity ever ran.
    assert _tool_starts(events, "add") == []


async def test_runtime_error_is_reported_as_text(client_and_queue):
    client, task_queue = client_and_queue
    # Type-valid but raises at runtime; a bad script is data, not a workflow failure.
    script = (
        "import asyncio\n"
        "async def main():\n"
        "    return 1 // 0\n"
        "asyncio.run(main())"
    )
    reply, _events = await _run(client, task_queue, script)
    assert "Script error" in reply
