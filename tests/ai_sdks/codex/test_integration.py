"""Exercise the Codex plugin through Temporal and a real JSONL subprocess.

The executable is deliberately paused mid-command so these tests distinguish
live activity publishing from returning a transcript after execution finishes.
No Codex credentials or model calls are required.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.codex import CodexConfig, CodexPlugin, CodexResult
from temporal_agent_harness.ai_sdks.codex.workflow import run_codex
from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    ToolApprovalPolicy,
)


@workflow.defn
@agent.defn
class CodexPluginProbe:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def ask(self, message: TextMessage) -> CodexResult:
        """Run Codex against the configured example workspace."""
        return await run_codex(message.text, runner=self._runner)


_FAKE_CODEX = r'''
import json
import pathlib
import sys
import time

root = pathlib.Path(__file__).parent
with (root / "invocations").open("a") as log:
    log.write("run\n")

def emit(event):
    print(json.dumps(event), flush=True)

emit({"type": "thread.started", "thread_id": "codex-test-thread"})
emit({"type": "turn.started"})
emit({
    "type": "item.started",
    "item": {
        "id": "command-1",
        "type": "command_execution",
        "command": "printf 'codex command result\\n'",
        "aggregated_output": "",
        "exit_code": None,
        "status": "in_progress",
    },
})
emit({
    "type": "item.updated",
    "item": {
        "id": "command-1",
        "type": "command_execution",
        "command": "printf 'codex command result\\n'",
        "aggregated_output": "working",
        "exit_code": None,
        "status": "in_progress",
    },
})

deadline = time.monotonic() + 30
while not (root / "release").exists():
    if time.monotonic() >= deadline:
        sys.exit("test did not release the fake Codex command")
    time.sleep(0.01)

if (root / "fail").exists():
    emit({"type": "turn.failed", "error": {"message": "fake Codex failure"}})
    sys.exit(1)

emit({
    "type": "item.completed",
    "item": {
        "id": "command-1",
        "type": "command_execution",
        "command": "printf 'codex command result\\n'",
        "aggregated_output": "codex command result\n",
        "exit_code": 0,
        "status": "completed",
    },
})
emit({
    "type": "item.completed",
    "item": {
        "id": "message-1",
        "type": "agent_message",
        "text": "Verified the example through Codex.",
    },
})
emit({
    "type": "turn.completed",
    "usage": {"input_tokens": 21, "cached_input_tokens": 5, "output_tokens": 8},
})
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text("Verified the example through Codex.")
(root / "finished").touch()
'''


@pytest_asyncio.fixture
async def codex_worker(tmp_path: Path):
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\n" + _FAKE_CODEX)
    executable.chmod(0o755)
    plugin = CodexPlugin(
        CodexConfig(
            workspace=str(tmp_path),
            codex_binary=str(executable),
            state_dir=str(tmp_path / "runs"),
            timeout_seconds=40,
        )
    )
    env = await WorkflowEnvironment.start_time_skipping(plugins=[plugin])
    queue = f"codex-plugin-{uuid.uuid4()}"
    try:
        # The plugin provides both the data converter and activity registration.
        # Explicitly registering the activity here would mask a broken plugin.
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[CodexPluginProbe],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            try:
                yield env.client, queue, plugin, tmp_path
            finally:
                # Release a paused process even if a stream assertion fails.
                (tmp_path / "release").touch()
    finally:
        await env.shutdown()


async def _start_probe(client: Client, queue: str):
    handle = await client.start_workflow(
        CodexPluginProbe.run,
        AgentConfig(),
        id=f"codex-probe-{uuid.uuid4()}",
        task_queue=queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="ask", payload={"text": "Inspect the example."}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    return handle


async def _observe_turn(
    client: Client,
    workflow_id: str,
    events: list[AgentEvent],
    command_started: asyncio.Event,
    progress_received: asyncio.Event,
) -> None:
    stream = WorkflowStreamClient.create(client, workflow_id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    ):
        envelope = item.data
        events.append(envelope)
        event = envelope.event
        if event.type == AgentEventType.TOOL_START and "command" in event.tool_input:
            command_started.set()
        if event.type == AgentEventType.TOOL_PROGRESS_DELTA:
            progress_received.set()
        if event.type == AgentEventType.TURN_END:
            return


async def test_plugin_streams_commands_before_completion_and_replays(codex_worker):
    client, queue, plugin, root = codex_worker
    handle = await _start_probe(client, queue)
    events: list[AgentEvent] = []
    command_started, progress_received = asyncio.Event(), asyncio.Event()
    observer = asyncio.create_task(
        _observe_turn(client, handle.id, events, command_started, progress_received)
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(command_started.wait(), progress_received.wait()),
            timeout=20,
        )
        assert not (root / "finished").exists()
        assert not any(e.event.type == AgentEventType.TURN_END for e in events)
        live_command = next(
            e.event
            for e in events
            if e.event.type == AgentEventType.TOOL_START
            and "command" in e.event.tool_input
        )
        assert any(
            e.event.type == AgentEventType.TOOL_PROGRESS_DELTA
            and e.event.tool_id == live_command.tool_id
            and "working" in e.event.progress_delta
            for e in events
        )

        (root / "release").touch()
        await asyncio.wait_for(observer, timeout=20)
        assert (root / "finished").exists()

        starts = [e.event for e in events if e.event.type == AgentEventType.TOOL_START]
        terminals = [
            e.event
            for e in events
            if e.event.type in (AgentEventType.TOOL_END, AgentEventType.TOOL_ERROR)
        ]
        assert len({e.tool_id for e in starts}) == len(starts)
        assert sorted(e.tool_id for e in starts) == sorted(e.tool_id for e in terminals)
        command = next(e for e in starts if "command" in e.tool_input)
        command_end = next(e for e in terminals if e.tool_id == command.tool_id)
        assert command_end.type == AgentEventType.TOOL_END
        assert "codex command result" in command_end.tool_output

        replies = [e.event for e in events if e.event.type == AgentEventType.REPLY]
        assert len(replies) == 1
        assert replies[0].output["text"] == "Verified the example through Codex."
        assert replies[0].output["thread_id"] == "codex-test-thread"
        assert replies[0].output["usage"]["input_tokens"] == 21
        assert replies[0].output["usage"]["output_tokens"] == 8

        await handle.terminate()
        history = await handle.fetch_history()
        await Replayer(
            workflows=[CodexPluginProbe],
            plugins=[plugin],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ).replay_workflow(history)
        assert (root / "invocations").read_text().splitlines() == ["run"]
    finally:
        (root / "release").touch()
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)


async def test_failed_codex_is_not_automatically_retried(codex_worker):
    client, queue, _, root = codex_worker
    (root / "fail").touch()
    (root / "release").touch()
    handle = await _start_probe(client, queue)
    events: list[AgentEvent] = []
    try:
        await asyncio.wait_for(
            _observe_turn(client, handle.id, events, asyncio.Event(), asyncio.Event()),
            timeout=20,
        )
        assert any(e.event.type == AgentEventType.ERROR for e in events)
        assert not any(e.event.type == AgentEventType.REPLY for e in events)
        assert (root / "invocations").read_text().splitlines() == ["run"]
        started = [e.event for e in events if e.event.type == AgentEventType.TOOL_START]
        errors = [e.event for e in events if e.event.type == AgentEventType.TOOL_ERROR]
        assert started
        assert sorted(e.tool_id for e in started) == sorted(e.tool_id for e in errors)

        history = await handle.fetch_history()
        codex_activities = [
            e.activity_task_scheduled_event_attributes
            for e in history.events
            if e.HasField("activity_task_scheduled_event_attributes")
            and e.activity_task_scheduled_event_attributes.activity_type.name
            == "codex_execute"
        ]
        assert len(codex_activities) == 1
        assert codex_activities[0].retry_policy.maximum_attempts == 1
    finally:
        await handle.terminate()
