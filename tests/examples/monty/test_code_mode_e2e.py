# ABOUTME: End-to-end test of the Code Mode harness feature with NO model in the loop. A
# model-free parent (CodeModeE2EParentWorkflow) builds a code_mode_tool over two deterministic
# activity tools and runs scripts through it, so the full stack — stub generation, the sandbox
# batch loop, host-call dispatch via run_tool (coercion + result marshalling + tool lifecycle),
# pre-run type checking, and replay — is exercised against real activities under a WorkflowEnvironment.
#
# Run with: uv run pytest tests/examples/monty/test_code_mode_e2e.py -v

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Any

import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    Interceptor,
    Replayer,
    StartActivityInput,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

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

from ._code_mode_e2e_parent import CODE_MODE_TOOLS, CodeModeE2EParentWorkflow


# Every activity each workflow scheduled, with its arguments, keyed by (workflow id, replaying):
# what a replay must reproduce exactly. Temporal's own replay check compares activity types and
# order but not arguments, so this is what catches a script whose values drift on replay.
_SCHEDULED: dict[tuple[str, bool], list[str]] = defaultdict(list)


class _RecordScheduledActivities(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _RecordingInbound


class _RecordingInbound(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_RecordingOutbound(outbound))


class _RecordingOutbound(WorkflowOutboundInterceptor):
    def start_activity(self, input: StartActivityInput) -> Any:
        key = (workflow.info().workflow_id, workflow.unsafe.is_replaying())
        _SCHEDULED[key].append(f"{input.activity}{input.args!r}")
        return super().start_activity(input)


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
        # The plugin supplies the durable bodies of the host tools; Code Mode needs no
        # activities of its own.
        plugins=[AgentHarnessPlugin(tools=CODE_MODE_TOOLS)],
        interceptors=[_RecordScheduledActivities()],
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
    reply, events, _handle = await _run_with_handle(client, task_queue, script)
    return reply, events


async def _replay(handle: WorkflowHandle[Any, Any]) -> None:
    """Replay the workflow's history from scratch, as a worker that lost it from its cache
    would, and require the replay to schedule exactly the activities the original run did,
    arguments included."""
    history = await handle.fetch_history()
    await Replayer(
        workflows=[CodeModeE2EParentWorkflow],
        data_converter=pydantic_data_converter,
        interceptors=[_RecordScheduledActivities()],
    ).replay_workflow(history)
    original = _SCHEDULED[(handle.id, False)]
    assert original, "the original run scheduled no activities"
    assert _SCHEDULED[(handle.id, True)] == original


async def _run_with_handle(
    client: Client, task_queue: str, script: str
) -> tuple[str, list[AgentEvent], WorkflowHandle[Any, Any]]:
    handle = await client.start_workflow(
        CodeModeE2EParentWorkflow.run,
        AgentConfig(),
        id=f"CodeModeE2EParent-{uuid.uuid4()}",
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
    return reply, events, handle


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


async def test_a_failed_host_call_raises_inside_the_script(client_and_queue):
    """A host call that fails raises at its await, so the script can handle it and go on; the
    other calls in its batch still complete, and the replay sees the same failure."""
    client, task_queue = client_and_queue
    script = (
        "import asyncio\n"
        "async def main():\n"
        "    async def safely(call):\n"
        "        try:\n"
        "            return await call\n"
        "        except Exception as e:\n"
        "            return str(e)\n"
        "    failed, added = await asyncio.gather(\n"
        '        safely(explode("no fuel")), safely(add({"a": 1, "b": 2}))\n'
        "    )\n"
        '    return [failed, added["total"]]\n'
        "asyncio.run(main())"
    )
    reply, _events, handle = await _run_with_handle(client, task_queue, script)
    assert "result: ['ApplicationError: no fuel', 3]" in reply, reply

    await _replay(handle)


async def test_an_uncaught_host_failure_ends_the_script_with_its_message(client_and_queue):
    client, task_queue = client_and_queue
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    return await explode("no fuel")\n'
        "asyncio.run(main())"
    )
    reply, _events = await _run(client, task_queue, script)
    assert reply.startswith("Script error ("), reply
    assert "ApplicationError: no fuel" in reply


# ---------------------------------------------------------------- durability


async def test_a_script_replays_to_the_same_host_calls(client_and_queue):
    """The script steps inside the workflow, so a replay re-runs it against the recorded host
    results. Everything it could observe from the worker comes from the workflow instead, so the
    replay makes the same host calls with the same arguments: randomness, the clock, set order, a
    durable sleep, a gathered batch, and a call made in one batch but awaited in a later one."""
    client, task_queue = client_and_queue
    script = (
        "import asyncio, datetime, random\n"
        "async def main():\n"
        '    later = greet({"name": "later"})\n'
        "    a, b = await asyncio.gather(\n"
        '        add({"a": random.randint(0, 10**6), "b": 1}), add({"a": 2, "b": 3})\n'
        "    )\n"
        "    await asyncio.sleep(1)\n"
        "    stamp = datetime.datetime.now().isoformat()\n"
        "    e = await echo(f\"{stamp}|{list({'z', 'y', 'x'})}|{random.random()}\")\n"
        "    g = await later\n"
        '    return [b["total"], g["message"], e["value"].endswith("s3cr3t")]\n'
        "asyncio.run(main())"
    )
    reply, events, handle = await _run_with_handle(client, task_queue, script)
    assert "result: [5, 'hi later', True]" in reply, reply
    assert len(_tool_starts(events, "add")) == 2

    await _replay(handle)


async def test_a_runaway_script_is_stopped_and_replays_without_running_again(client_and_queue):
    """A script that never awaits again is stopped at the step time limit, and the stop is
    recorded: a replay returns the same error without re-running the loop, which would otherwise
    decide afresh, on a different machine, whether it overran."""
    client, task_queue = client_and_queue
    script = (
        "import asyncio\n"
        "async def main():\n"
        '    await add({"a": 1, "b": 2})\n'
        "    while True:\n"
        "        pass\n"
        "asyncio.run(main())"
    )
    reply, _events, handle = await _run_with_handle(client, task_queue, script)
    assert reply.startswith("Script error (TimeoutError: the script ran for more than 1s"), reply

    history = await handle.fetch_history()
    markers = [
        e.marker_recorded_event_attributes
        for e in history.events
        if e.HasField("marker_recorded_event_attributes")
    ]
    assert len(markers) == 1

    started = time.monotonic()
    await _replay(handle)
    assert time.monotonic() - started < 1, "the replay re-ran the runaway step"
