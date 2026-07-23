# ABOUTME: Tests for `@agent.activity_tool_defn(sandboxed=True)` — running a tool's body inside
# a remote-box sandbox instead of directly in the worker process. Uses remote-box's Subprocess
# backend throughout (no API keys / real sandbox provider needed): it still spawns a genuinely
# separate OS process per call, so a distinct pid is real proof the tool body ran out-of-process,
# not just a mock. Skipped entirely if the optional `sandbox` extra isn't installed.
#
# Run with: uv run pytest tests/harness/test_sandboxed_tools.py -v
#
# Deliberately does NOT use `from __future__ import annotations`: activity_body's
# __annotations__ resolution (temporalio's activity.defn -> get_type_hints) uses the closure's
# OWN __globals__ (agent_workflow.py's), not this file's — a tool with a custom model type in a
# module using stringized annotations hits that pre-existing gap. Unrelated to sandboxing itself
# (any activity_tool_defn tool with a non-builtin type would hit it), so worked around here rather
# than fixed in this PR.

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("remote")

import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from remote import Subprocess

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
    AgentMessageReply,
)
from temporal_agent_harness.harness.agent_workflow import _validate_sandboxable, _tool_signatures
from temporal_agent_harness.harness.sandbox import SandboxConfig, build_sandbox, check_sandbox
from temporal_agent_harness.harness.sandbox.activities import (
    SANDBOX_ACTIVITIES,
    _SESSIONS,
    get_or_resume_session,
)

from _sandboxed_tool_fixtures import PidInput, PidResult, get_sandbox_pid, get_sandbox_pid_2
from _real_sandbox_workflow_fixtures import (
    RealSandboxedWorkflowRunnerProbeAgent,
    real_sandbox_probe,
)

_HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Probe workflows
# ---------------------------------------------------------------------------


@workflow.defn
@agent.defn
class SandboxedToolAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            sandbox=SandboxConfig(backend=Subprocess(), local_project_root=_HERE),
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def probe(self, message: TextMessage) -> TextReply:
        """Run two sandboxed tools and reply with both pids."""
        result = await self._runner.run_tool("t1", get_sandbox_pid, PidInput())
        result2 = await self._runner.run_tool("t2", get_sandbox_pid_2, PidInput())
        return TextReply(text=f"{result.pid},{result2.pid}")


@workflow.defn
@agent.defn
class MisconfiguredSandboxAgent:
    """Uses a sandboxed=True tool but never configures sandbox= — must raise cleanly."""

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
    async def probe(self, message: TextMessage) -> TextReply:
        """Run a sandboxed tool with no sandbox configured — expect a clean error."""
        result = await self._runner.run_tool("t1", get_sandbox_pid, PidInput())
        return TextReply(text=str(result.pid))


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"sandboxed-tool-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SandboxedToolAgent, MisconfiguredSandboxAgent],
        activities=[
            *SANDBOX_ACTIVITIES,
            agent.tool_activity(get_sandbox_pid),
            agent.tool_activity(get_sandbox_pid_2),
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _collect_until_turn_end(client: Client, workflow_id: str) -> list[AgentEvent]:
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[AgentEvent] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
    ):
        envelope: AgentEvent = item.data
        events.append(envelope)
        if envelope.event.type == AgentEventType.TURN_END:
            break
    return events


# ---------------------------------------------------------------------------
# End-to-end: sandboxed tool actually runs out-of-process
# ---------------------------------------------------------------------------


async def test_sandboxed_tool_runs_in_a_real_subprocess(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        SandboxedToolAgent.run,
        AgentConfig(),
        id=f"SandboxedToolAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe", payload={"text": "hi"}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    events = await _collect_until_turn_end(client, handle.id)
    replies = [e.event for e in events if e.event.type == AgentEventType.REPLY]
    assert len(replies) == 1
    pid1_str, pid2_str = replies[0].output["text"].split(",")
    pid1, pid2 = int(pid1_str), int(pid2_str)

    # Both sandboxed tools ran in a genuinely separate OS process (proves out-of-process
    # execution, not just a mock), and share one agent-level sandbox (one SandboxConfig, not
    # per-tool backend wiring) so both calls used the SAME subprocess-backend session lookup.
    assert pid1 != os.getpid()
    assert pid2 != os.getpid()

    await handle.signal("close")
    await handle.result()


# ---------------------------------------------------------------------------
# Misconfiguration: sandboxed=True tool, no sandbox= configured
# ---------------------------------------------------------------------------


async def test_sandboxed_tool_without_sandbox_config_raises_clean_error(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        MisconfiguredSandboxAgent.run,
        AgentConfig(),
        id=f"MisconfiguredSandboxAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe", payload={"text": "hi"}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    events = await _collect_until_turn_end(client, handle.id)
    errors = [e.event for e in events if e.event.type == AgentEventType.ERROR]
    assert len(errors) == 1
    assert "SandboxNotConfigured" in errors[0].message
    assert "no sandbox backend" in errors[0].message

    await handle.signal("close")
    await handle.result()


# ---------------------------------------------------------------------------
# Lifecycle: activate at turn start, terminate on close
# ---------------------------------------------------------------------------


async def test_sandbox_activates_on_turn_and_terminates_on_close(client_and_queue):
    client, task_queue = client_and_queue
    handle = await client.start_workflow(
        SandboxedToolAgent.run,
        AgentConfig(),
        id=f"SandboxedToolAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    assert len(_SESSIONS) == 0

    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe", payload={"text": "hi"}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    await _collect_until_turn_end(client, handle.id)
    # The turn activated (and, since no next message is queued, paused) the sandbox — the
    # worker-process-local session cache should hold exactly one live session for this run.
    assert len(_SESSIONS) == 1

    await handle.signal("close")
    await handle.result()
    # The outer run() finally unconditionally terminated the sandbox on close.
    assert len(_SESSIONS) == 0


# ---------------------------------------------------------------------------
# require_prebuilt / build_sandbox / check_sandbox
# ---------------------------------------------------------------------------


def test_build_and_check_sandbox_subprocess_backend_is_always_ready():
    config = SandboxConfig(backend=Subprocess(), local_project_root=_HERE)
    built = build_sandbox(config)
    assert built.status in ("built", "ready")
    checked = check_sandbox(config)
    assert checked.status == "ready"


# ---------------------------------------------------------------------------
# get_or_resume_session: cache hit vs cache-miss resume (pure unit test)
# ---------------------------------------------------------------------------


async def test_get_or_resume_session_caches_by_workflow_run_id(monkeypatch):
    """Not run inside a real activity, so activity.info() isn't available — patch it to a fake
    run id to unit-test the cache-hit path directly."""
    from temporalio import activity

    class _FakeInfo:
        workflow_run_id = f"fake-run-{uuid.uuid4()}"

    monkeypatch.setattr(activity, "info", lambda: _FakeInfo())
    key = _FakeInfo.workflow_run_id
    assert key not in _SESSIONS

    backend = Subprocess()
    session1 = await get_or_resume_session(None, backend, _HERE)
    assert key in _SESSIONS
    session2 = await get_or_resume_session(None, backend, _HERE)
    assert session2 is session1  # cache hit — no second RemoteSession constructed

    del _SESSIONS[key]


# ---------------------------------------------------------------------------
# _validate_sandboxable: decoration-time constraint enforcement (pure unit tests)
# ---------------------------------------------------------------------------


def test_validate_sandboxable_rejects_multi_param_tool():
    async def two_params(a: PidInput, b: PidInput) -> PidResult: ...

    sig = _tool_signatures(two_params)
    with pytest.raises(ValueError, match="exactly one parameter"):
        _validate_sandboxable(two_params, sig, "two_params")


def test_validate_sandboxable_rejects_non_basemodel_param():
    async def bad_param(x: str) -> PidResult: ...

    sig = _tool_signatures(bad_param)
    with pytest.raises(ValueError, match="BaseModel subclass"):
        _validate_sandboxable(bad_param, sig, "bad_param")


def test_validate_sandboxable_rejects_non_basemodel_return():
    async def bad_return(arg: PidInput) -> str: ...

    sig = _tool_signatures(bad_return)
    with pytest.raises(ValueError, match="return type must be"):
        _validate_sandboxable(bad_return, sig, "bad_return")


def test_validate_sandboxable_accepts_well_formed_tool():
    async def fine(arg: PidInput) -> PidResult: ...

    sig = _tool_signatures(fine)
    _validate_sandboxable(fine, sig, "fine")  # must not raise


def test_sandboxed_activity_tool_defn_rejects_bad_shape_at_decoration_time():
    with pytest.raises(ValueError, match="exactly one parameter"):

        @agent.activity_tool_defn(sandboxed=True)
        async def two_args(a: PidInput, b: PidInput) -> PidResult: ...


# ---------------------------------------------------------------------------
# Regression: the REAL (default) SandboxedWorkflowRunner, not UnsandboxedWorkflowRunner
# ---------------------------------------------------------------------------
#
# Every other test in this file (matching this whole codebase's established test convention)
# runs under UnsandboxedWorkflowRunner. That's necessary for MANY existing tests, but it also
# means this suite would never catch a regression that only manifests under Temporal's real
# workflow determinism sandbox — which is exactly what production workers use by default. Two
# such regressions were found and fixed while building this feature:
#   1. `dispatch()` unconditionally called `os.environ.get(...)` (via `_in_remote_execution()`) —
#      a RESTRICTED operation under real sandboxed execution — breaking every activity tool call
#      (sandboxed or not) for any agent whose workflow module doesn't wrap harness imports in
#      `imports_passed_through()`. Now gated on `sandboxed` — see agent_workflow.py's dispatch().
#   2. A workflow module importing `agent_protocol` OUTSIDE its `imports_passed_through()` block
#      — even with `agent`/`AgentWorkflowRunner`/the tool module wrapped together — silently
#      split `agent_workflow.py` into two loaded copies with two different `_CURRENT_RUNNER`
#      contextvars, so `run_tool` (set on one copy) became invisible to a sandboxed tool's
#      approval-policy check (read on the other). See SandboxConfig's docstring.
# This test exercises the real runner end to end so a future change reintroducing either class of
# bug fails here, not just in a manually-run interactive example.


@pytest_asyncio.fixture
async def real_sandboxed_client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"real-sandboxed-runner-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[RealSandboxedWorkflowRunnerProbeAgent],
        activities=[*SANDBOX_ACTIVITIES, agent.tool_activity(real_sandbox_probe)],
        # Deliberately NO workflow_runner override — Temporal's real default SandboxedWorkflowRunner.
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def test_sandboxed_tool_works_under_real_sandboxed_workflow_runner(
    real_sandboxed_client_and_queue,
):
    client, task_queue = real_sandboxed_client_and_queue
    handle = await client.start_workflow(
        RealSandboxedWorkflowRunnerProbeAgent.run,
        AgentConfig(),
        id=f"RealSandboxedWorkflowRunnerProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe", payload={"text": "hi"}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    events = await _collect_until_turn_end(client, handle.id)
    errors = [e.event for e in events if e.event.type == AgentEventType.ERROR]
    replies = [e.event for e in events if e.event.type == AgentEventType.REPLY]
    assert not errors, errors
    assert len(replies) == 1

    await handle.signal("close")
    await handle.result()
