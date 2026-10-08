# End-to-end tests for agent sandboxes (temporal_agent_harness.harness.sandbox), run on the
# time-skipping test server against OpenAI's real unix_local sandbox backend: no network.

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import json
import re
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from agents.sandbox import LocalSnapshotSpec
from agents.sandbox.capabilities import Capability, Shell
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from temporal_agent_harness.ai_sdks.openai_agents import OpenAIAgentsPlugin
from temporal_agent_harness.ai_sdks.openai_agents.testing import (
    ResponseBuilders,
    TestModel,
    TestModelProvider,
)
from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.agent import Injected
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.harness.code_mode.stubs import render_host_interface
from temporal_agent_harness.harness.sandbox import (
    IdlePolicy,
    SandboxClientProvider,
    SandboxConfig,
    SandboxImage,
    SandboxSession,
    sandbox_activities,
)
from temporal_agent_harness.harness.sandbox._lifecycle import SandboxLifecycle
from temporal_agent_harness.harness.sandbox.capabilities import OUTPUT_BYTES, SPILL_DIR
from temporal_agent_harness.harness.sandbox.tools import tools_for
from tests.harness import _sandbox_fixtures as fx

_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules("agents", "openai")
)


def _activities(provider: SandboxClientProvider) -> list[Any]:
    return [
        *sandbox_activities([provider]),
        agent.tool_activity(fx.run_in_sandbox),
        agent.tool_activity(fx.serving_task_queue),
    ]


@pytest_asyncio.fixture
async def env():
    shutil.rmtree(fx.SNAPSHOT_DIR, ignore_errors=True)
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    try:
        yield env
    finally:
        await env.shutdown()
        shutil.rmtree(fx.SNAPSHOT_DIR, ignore_errors=True)


def _worker(client: Client, task_queue: str, provider: SandboxClientProvider) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[fx.SandboxProbeAgent, fx.NoSandboxAgent, fx.PinnedProbeAgent, fx.NotesAgent],
        activities=_activities(provider),
        workflow_runner=_RUNNER,
    )


async def _send(client: Client, handle: WorkflowHandle[Any, Any], kind: str, text: str) -> str:
    """Send one message and return its handler's reply text."""
    reply = await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type=kind, payload={"text": text}),
        result_type=AgentMessageReply,
    )
    stream = WorkflowStreamClient.create(client, handle.id)
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=reply.accepted_offset,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    ):
        event = item.data.event
        if event.type == AgentEventType.MESSAGE_HANDLER_END:
            return event.output["text"]
        if event.type == AgentEventType.MESSAGE_HANDLER_ERROR:
            raise AssertionError(event.message)
    raise AssertionError("stream ended without a reply")


async def _start(client: Client, task_queue: str, workflow: Any) -> WorkflowHandle[Any, Any]:
    return await client.start_workflow(
        workflow.run, AgentConfig(), id=f"sandbox-{uuid.uuid4()}", task_queue=task_queue
    )


def _snapshots() -> list[str]:
    return sorted(p.name for p in fx.SNAPSHOT_DIR.glob("*.tar")) if fx.SNAPSHOT_DIR.exists() else []


async def test_shell_output_is_cut_inside_the_sandbox_and_kept_in_a_log(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    async with _worker(env.client, "sbx", provider):
        handle = await _start(env.client, "sbx", fx.SandboxProbeAgent)

        async def shell(**args: Any) -> str:
            return await _send(env.client, handle, "shell", json.dumps(args))

        reply = await shell(cmd="seq 1 100000; exit 3")
        assert "Process exited with code 3" in reply
        assert "[100000 lines, 588895 bytes, cut to the first 4000 and last 6000 bytes." in reply
        assert "\n1\n2\n3\n" in reply
        assert reply.rstrip().endswith("\n100000")
        assert "\n50000\n" not in reply
        assert len(reply) < OUTPUT_BYTES
        # unix_local runs on this machine, so the sandbox's /tmp is this /tmp.
        log = Path(re.search(r"All of it is in (\S+):", reply).group(1))
        assert log.parent == Path(str(SPILL_DIR))
        assert log.read_text().splitlines() == [str(n) for n in range(1, 100001)]
        log.unlink()

        small = await shell(cmd="printf '\\033[31mred\\033[0m\\n'; echo two >&2")
        assert "Process exited with code 0" in small
        assert "Output:\nred\ntwo" in small
        assert "All of it is in" not in small

        running = await shell(
            cmd="python3 -c 'import time; print(\"started\"); time.sleep(2)'; seq 1 3",
            yield_time_ms=1_000,
        )
        session_id = re.search(r"Process running with session ID (\d+)", running).group(1)
        log = Path(re.search(r"Its output goes to (\S+);", running).group(1))
        assert log.read_text() == "started\n"
        done = await shell(session_id=int(session_id), yield_time_ms=5_000)
        assert "Process exited with code 0" in done
        assert "started\n1\n2\n3" in done

        # The worker's PtySessionNotFoundError reaches OpenAI's tool, in the workflow, as
        # itself, so the tool answers the model instead of failing.
        gone = await shell(session_id=int(session_id), yield_time_ms=0)
        assert "write_stdin failed: PTY session not found" in gone, gone
        await handle.signal("close")
        await handle.result()


async def test_tools_share_one_lazily_created_sandbox_that_idles_resumes_and_closes(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    async with _worker(env.client, "sbx", provider):
        handle = await _start(env.client, "sbx", fx.SandboxProbeAgent)

        results = ast.literal_eval(await _send(env.client, handle, "write_files", "hello"))
        assert results["custom"] == "hello"
        assert "hello" in results["exec_command"]
        assert "Process exited with code 0" in results["exec_command"]
        assert "b.txt" in results["apply_patch"]
        assert results["view_image"] == "[image: image/png, 12 bytes]"
        assert set(results["inline"].split()) >= {"a.txt", "b.txt", "img.png"}
        assert len(provider._sessions) == 1
        root = next(iter(provider._sessions.values())).state.manifest.root

        # Idle: the workspace goes to the snapshot and the sandbox is shut down.
        assert _snapshots() == []
        await env.sleep(fx.IDLE_AFTER + timedelta(seconds=1))
        assert len(_snapshots()) == 1

        # The next use resumes it, workspace intact.
        assert await _send(env.client, handle, "read_file", "b.txt") == "bee"

        await handle.signal("close")
        await handle.result()
    # Closed: the sandbox is deleted and, with keep_snapshot off, so is its snapshot.
    assert _snapshots() == []
    assert provider._sessions == {}
    assert not __import__("os").path.exists(root)


async def test_a_sandbox_tool_on_an_agent_without_a_sandbox_fails_cleanly(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    async with _worker(env.client, "nosbx", provider):
        handle = await _start(env.client, "nosbx", fx.NoSandboxAgent)
        reply = await _send(env.client, handle, "try_tool", "")
        assert reply.startswith("error:") and "SandboxNotConfigured" in reply, reply
        assert provider._sessions == {}


async def test_an_affinity_queue_pins_the_session_and_unpins_when_its_worker_is_gone(env):
    provider = SandboxClientProvider(
        fx.PROVIDER, UnixLocalSandboxClient(), affinity_task_queue="sbx-this-worker"
    )
    async with _worker(env.client, "sbx-shared", provider):
        handle = await _start(env.client, "sbx-shared", fx.PinnedProbeAgent)
        pinned = Worker(
            env.client, task_queue="sbx-this-worker", activities=_activities(provider)
        )
        async with pinned:
            assert await _send(env.client, handle, "where", "") == "sbx-this-worker"
        # The pinned worker is gone: the call fails cleanly, and the next is served from the
        # shared queue, resuming the session there.
        reply = await _send(env.client, handle, "where", "")
        assert "SandboxWorkerLost" in reply, reply
        assert await _send(env.client, handle, "where", "") == "sbx-shared"
        await handle.signal("close")
        await handle.result()


async def test_a_malformed_tool_call_fails_back_to_the_caller_without_retrying(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    async with _worker(env.client, "sbx-bad", provider):
        handle = await _start(env.client, "sbx-bad", fx.SandboxProbeAgent)
        # Retried, these would never come back: the same patch fails the same way every time.
        results = ast.literal_eval(
            await asyncio.wait_for(_send(env.client, handle, "bad_patches", ""), 60)
        )
        assert "Invalid Add File line: sea" in results["unprefixed_line"]
        assert "must start with '*** Begin Patch'" in results["no_begin_marker"]
        attempts = [
            e.activity_task_started_event_attributes.attempt
            async for e in handle.fetch_history_events()
            if e.HasField("activity_task_started_event_attributes")
        ]
        assert attempts and set(attempts) == {1}, attempts
        await handle.signal("close")
        await handle.result()


async def test_apply_patch_repairs_add_file_mistakes_models_repeat(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    async with _worker(env.client, "sbx-repair", provider):
        handle = await _start(env.client, "sbx-repair", fx.SandboxProbeAgent)
        results = ast.literal_eval(await _send(env.client, handle, "repairable_patches", ""))
        assert results["plus_end_marker"] == "Created c.txt"
        assert results["empty_files"].splitlines() == [
            "Created pkg/__init__.py",
            "Created pkg/mod.py",
            "Created empty.txt",
        ]
        assert results["contents"].split() == ["0", "0", "sea", "x", "=", "1"]
        await handle.signal("close")
        await handle.result()


async def test_runner_offers_capability_tools_and_instructions(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    async with _worker(env.client, "sbx-instr", provider):
        handle = await _start(env.client, "sbx-instr", fx.SandboxProbeAgent)
        names, _, instructions = (await _send(env.client, handle, "instructions", "")).partition(
            "\n"
        )
        assert ast.literal_eval(names) == ["exec_command", "write_stdin", "view_image", "apply_patch"]
        assert "exec_command" in instructions
        # Asking for tools and instructions creates nothing.
        assert provider._sessions == {}
        await handle.signal("close")
        await handle.result()


async def test_an_openai_sandbox_agent_runs_on_the_runners_sandbox_across_turns(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    model = TestModel.returning_responses(
        [
            ResponseBuilders.tool_call('{"cmd": "printf one > notes.txt"}', "exec_command"),
            ResponseBuilders.output_message("wrote it"),
            ResponseBuilders.tool_call('{"cmd": "printf two >> notes.txt"}', "exec_command"),
            ResponseBuilders.output_message("appended"),
        ]
    )
    plugin = OpenAIAgentsPlugin(
        model_provider=TestModelProvider(model), sandbox_clients=[provider]
    )
    config = env.client.config()
    # The plugin installs its own (pydantic-based) payload converter over the default one.
    client = Client(
        **{**config, "data_converter": DataConverter.default, "plugins": [plugin]}
    )
    async with Worker(
        client,
        task_queue="sbx-openai",
        workflows=[fx.OpenAISandboxAgent],
        activities=[agent.tool_activity(fx.run_in_sandbox)],
        workflow_runner=_RUNNER,
    ):
        handle = await _start(client, "sbx-openai", fx.OpenAISandboxAgent)
        assert await _send(client, handle, "run_agent", "write notes") == "wrote it"
        assert await _send(client, handle, "run_agent", "append notes") == "appended"
        # One sandbox, kept across both runs and shared with the harness's own tools.
        assert await _send(client, handle, "read_file", "notes.txt") == "onetwo"
        assert len(provider._sessions) == 1
        await handle.signal("close")
        await handle.result()
    assert provider._sessions == {}


async def test_a_developers_own_capability_gets_its_tools_instructions_and_manifest(env):
    provider = SandboxClientProvider(fx.PROVIDER, UnixLocalSandboxClient())
    async with _worker(env.client, "sbx-notes", provider):
        handle = await _start(env.client, "sbx-notes", fx.NotesAgent)
        results = ast.literal_eval(await _send(env.client, handle, "count", "NOTES.md"))
        assert results["names"] == ["count_lines"]
        assert results["instructions"] == "Count lines with count_lines."
        # NOTES.md exists because the capability's process_manifest added it.
        assert results["counted"] == "NOTES.md: 2 lines"
        await handle.signal("close")
        await handle.result()


def _capability_tools() -> dict[str, Any]:
    lifecycle = SandboxLifecycle(fx.SANDBOX)
    return {t.__name__: t for t in tools_for(lifecycle.capabilities, lifecycle.ensure_running)}


def test_capability_tools_take_their_parameters_from_the_tools_own_schema():
    tools = _capability_tools()
    assert list(tools) == ["exec_command", "write_stdin", "view_image", "apply_patch"]
    exec_params = inspect.signature(tools["exec_command"]).parameters
    assert list(exec_params) == [
        "cmd", "workdir", "shell", "login", "tty", "yield_time_ms", "max_output_tokens"
    ]
    assert exec_params["cmd"].default is inspect.Parameter.empty
    assert exec_params["yield_time_ms"].default == 10_000
    assert list(inspect.signature(tools["apply_patch"]).parameters) == ["patch"]
    assert "FREEFORM" not in (tools["apply_patch"].__doc__ or "")
    stubs = render_host_interface(list(tools.values()))
    assert "def exec_command(" in stubs and "cmd: str" in stubs


def test_injected_sandbox_session_is_hidden_from_the_model_like_any_injection():
    assert list(inspect.signature(fx.run_in_sandbox).parameters) == ["command"]
    assert "session:" not in render_host_interface([fx.run_in_sandbox])
    # ... and from the activity's own arguments, since a live session can't be serialized.
    activity_params = inspect.signature(agent.tool_activity(fx.run_in_sandbox)).parameters
    assert list(activity_params) == ["command", "tool_ctx"]


def test_gemini_function_results_carry_sandbox_images_as_image_blocks():
    from temporal_agent_harness.ai_sdks.google_genai_plugin import function_result_value

    png = SandboxImage(mime_type="image/png", data="iVBORw0K")
    assert function_result_value(png) == [
        {"type": "image", "data": "iVBORw0K", "mime_type": "image/png"}
    ]
    svg = SandboxImage(mime_type="image/svg+xml", data="PHN2Zz4=")
    assert function_result_value(svg) == "[image: image/svg+xml, 6 bytes]"
    assert function_result_value({"a": 1}) == "{'a': 1}"


def test_at_most_one_sandbox_session_per_tool():
    async def two(a: Injected[SandboxSession], b: Injected[SandboxSession]) -> str: ...

    with pytest.raises(TypeError, match="one"):
        agent.activity_tool_defn()(two)


def test_config_rejects_unsafe_settings_and_missing_required_capabilities():
    with pytest.raises(ValueError, match="LocalSnapshotSpec"):
        SandboxConfig(client="x", snapshot=LocalSnapshotSpec(base_path=fx.SNAPSHOT_DIR))
    with pytest.raises(ValueError, match="pause_on_exit"):
        SandboxConfig(client="x", idle=IdlePolicy(after=timedelta(minutes=1), action="pause"))

    class NeedsShell(Capability):
        type: str = "needs_shell"

        def required_capability_types(self) -> set[str]:
            return {"shell"}

    with pytest.raises(ValueError, match="NeedsShell requires missing capabilities: shell"):
        SandboxConfig(client="x", capabilities=[NeedsShell()])
    SandboxConfig(client="x", capabilities=[NeedsShell(), Shell()])
    assert dataclasses.is_dataclass(SandboxConfig)
