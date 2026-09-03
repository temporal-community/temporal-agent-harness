# ABOUTME: Tests for `@agent.activity_tool_defn(sandboxed=True)` — running a tool's body inside
# a sandbox instead of directly in the worker process. Uses `backend="local"` throughout (runs
# tool bodies in-process on the worker — no msb/microVM required in CI).
#
# Run with: uv run pytest tests/harness/test_sandboxed_tools.py -v

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

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
    SandboxActivities,
    get_or_resume_session,
)
from temporal_agent_harness.harness.sandbox.config import MicrosandboxBackend
from temporal_agent_harness.harness.sandbox.models import (
    SANDBOX_ACTIVATE_ACTIVITY,
    SANDBOX_PAUSE_ACTIVITY,
    SANDBOX_TERMINATE_ACTIVITY,
    SandboxActivateInput,
    SandboxPauseInput,
    SandboxRefResult,
    SandboxTerminateInput,
)
from temporal_agent_harness.harness.sandbox_ref import SandboxRef

from _sandboxed_tool_fixtures import PidInput, PidResult, get_sandbox_pid, get_sandbox_pid_2
from _real_sandbox_workflow_fixtures import (
    RealSandboxedWorkflowRunnerProbeAgent,
    real_sandbox_probe,
)

_HERE = Path(__file__).parent
_LOCAL = SandboxConfig(backend="local", local_project_root=_HERE)


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
            sandbox=_LOCAL,
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


@pytest_asyncio.fixture(autouse=True)
def _clear_sandbox_session_cache():
    _SESSIONS.clear()
    yield
    _SESSIONS.clear()


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
# End-to-end: sandboxed tool runs via local backend (in-process subprocess harness)
# ---------------------------------------------------------------------------


async def test_sandboxed_tool_runs_via_local_backend(client_and_queue):
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

    # Local backend runs tools in-process on the worker — same pid for both tools.
    assert pid1 == os.getpid()
    assert pid2 == pid1

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
    assert len(_SESSIONS) == 1

    await handle.signal("close")
    await handle.result()
    assert len(_SESSIONS) == 0


# ---------------------------------------------------------------------------
# require_prebuilt / build_sandbox / check_sandbox
# ---------------------------------------------------------------------------


def test_build_and_check_sandbox_local_backend_is_always_ready():
    built = build_sandbox(_LOCAL)
    assert built.status in ("built", "ready")
    checked = check_sandbox(_LOCAL)
    assert checked.status == "ready"


# ---------------------------------------------------------------------------
# get_or_resume_session: cache hit vs cache-miss resume (pure unit test)
# ---------------------------------------------------------------------------


async def test_get_or_resume_session_caches_by_workflow_run_id(monkeypatch):
    from temporalio import activity

    class _FakeInfo:
        workflow_run_id = f"fake-run-{uuid.uuid4()}"

    monkeypatch.setattr(activity, "info", lambda: _FakeInfo())
    key = _FakeInfo.workflow_run_id
    assert key not in _SESSIONS

    session1 = await get_or_resume_session(None, "local", _HERE, workflow_run_id=key)
    assert key in _SESSIONS
    session2 = await get_or_resume_session(None, "local", _HERE, workflow_run_id=key)
    assert session2 is session1

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
    _validate_sandboxable(fine, sig, "fine")


def test_sandboxed_activity_tool_defn_rejects_bad_shape_at_decoration_time():
    with pytest.raises(ValueError, match="exactly one parameter"):

        @agent.activity_tool_defn(sandboxed=True)
        async def two_args(a: PidInput, b: PidInput) -> PidResult: ...


# ---------------------------------------------------------------------------
# SandboxConfig(backend="<provider name>"): provider registry + workflow threading
# ---------------------------------------------------------------------------


async def test_resolve_backend_returns_the_config_the_provider_produced():
    async def mint() -> MicrosandboxBackend:
        return MicrosandboxBackend(snapshot_name="probe", secrets={"MINTED": "tok-123"})

    activities = SandboxActivities({"mint": mint})
    resolved = await activities._resolve_backend("mint")

    assert isinstance(resolved, MicrosandboxBackend)
    assert resolved.snapshot_name == "probe"
    assert resolved.secrets == {"MINTED": "tok-123"}


async def test_resolve_backend_rejects_unregistered_provider_name():
    async def mint() -> MicrosandboxBackend:
        return MicrosandboxBackend(snapshot_name="x")

    activities = SandboxActivities({"registered-one": mint})
    with pytest.raises(ApplicationError) as excinfo:
        await activities._resolve_backend("typo")

    assert excinfo.value.type == "SandboxBackendProviderNotRegistered"
    assert excinfo.value.non_retryable
    assert "registered-one" in str(excinfo.value)


async def test_resolve_backend_rejects_provider_returning_non_backend_config():
    async def junk() -> dict:
        return {"type": "microsandbox"}

    activities = SandboxActivities({"junk": junk})
    with pytest.raises(ApplicationError) as excinfo:
        await activities._resolve_backend("junk")

    assert excinfo.value.type == "SandboxBackendProviderInvalidResult"
    assert excinfo.value.non_retryable


async def test_sandbox_activate_runs_named_provider_and_echoes_its_backend():
    calls = 0

    async def provider():
        nonlocal calls
        calls += 1
        return "local"

    activities = SandboxActivities({"probe-provider": provider})
    env = ActivityEnvironment()
    result = await env.run(
        activities.sandbox_activate,
        SandboxActivateInput(
            backend="probe-provider",
            local_project_root=str(_HERE),
        ),
    )

    assert calls == 1
    assert result.backend == {"type": "local"}
    assert result.ref.backend == "LOCAL"

    await env.run(
        activities.sandbox_terminate,
        SandboxTerminateInput(
            ref=result.ref,
            backend={"type": "local"},
            local_project_root=str(_HERE),
        ),
    )


async def test_sandbox_activate_with_local_backend_echoes_none():
    activities = SandboxActivities()
    env = ActivityEnvironment()
    result = await env.run(
        activities.sandbox_activate,
        SandboxActivateInput(
            backend={"type": "local"},
            local_project_root=str(_HERE),
        ),
    )

    assert result.backend is None
    assert result.ref.backend == "LOCAL"

    await env.run(
        activities.sandbox_terminate,
        SandboxTerminateInput(
            ref=result.ref,
            backend={"type": "local"},
            local_project_root=str(_HERE),
        ),
    )


_PROVIDER_NAME = "probe-provider"

_PROVIDED_BACKEND = MicrosandboxBackend(
    snapshot_name="provided", secrets={"MINTED": "tok-123"}
).model_dump(mode="json")

_activate_inputs: list[SandboxActivateInput] = []
_pause_inputs: list[SandboxPauseInput] = []
_terminate_inputs: list[SandboxTerminateInput] = []


@activity.defn(name=SANDBOX_ACTIVATE_ACTIVITY)
async def stub_activate(input: SandboxActivateInput) -> SandboxRefResult:
    _activate_inputs.append(input)
    return SandboxRefResult(
        ref=SandboxRef(backend="MICROSANDBOX", sandbox_id="stub-sandbox"),
        backend=_PROVIDED_BACKEND if isinstance(input.backend, str) else None,
    )


@activity.defn(name=SANDBOX_PAUSE_ACTIVITY)
async def stub_pause(input: SandboxPauseInput) -> SandboxRefResult:
    _pause_inputs.append(input)
    return SandboxRefResult(ref=input.ref)


@activity.defn(name=SANDBOX_TERMINATE_ACTIVITY)
async def stub_terminate(input: SandboxTerminateInput) -> None:
    _terminate_inputs.append(input)


async def _wait_until(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = timeout / 0.05
    while deadline > 0:
        if predicate():
            return
        await asyncio.sleep(0.05)
        deadline -= 1
    raise AssertionError(f"timed out waiting for {what}")


@workflow.defn
@agent.defn
class ProvidedBackendAgent:
    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
            sandbox=SandboxConfig(backend=_PROVIDER_NAME, local_project_root=_HERE),
        )

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def probe(self, message: TextMessage) -> TextReply:
        """Reply without touching a tool — lifecycle threading test only."""
        return TextReply(text="ok")


async def test_named_provider_runs_once_and_its_backend_is_threaded_through_the_run():
    _activate_inputs.clear()
    _pause_inputs.clear()
    _terminate_inputs.clear()
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"stub-sandbox-lifecycle-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ProvidedBackendAgent],
        activities=[stub_activate, stub_pause, stub_terminate],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            ProvidedBackendAgent.run,
            AgentConfig(),
            id=f"ProvidedBackendAgent-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        for turn in (1, 2):
            await handle.execute_update(
                SEND_AGENT_MESSAGE_UPDATE,
                AgentMessage(type="probe", payload={"text": "hi"}, expected_turn=turn),
                result_type=AgentMessageReply,
            )
            await _wait_until(lambda: len(_pause_inputs) == turn, f"turn {turn} to pause")
        await handle.signal("close")
        await handle.result()
    await env.shutdown()

    assert len(_activate_inputs) == 2
    assert _activate_inputs[0].backend == _PROVIDER_NAME
    assert _activate_inputs[1].backend == _PROVIDED_BACKEND
    assert _pause_inputs and all(i.backend == _PROVIDED_BACKEND for i in _pause_inputs)
    assert len(_terminate_inputs) == 1
    assert _terminate_inputs[0].backend == _PROVIDED_BACKEND


@pytest_asyncio.fixture
async def real_sandboxed_client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    task_queue = f"real-sandboxed-runner-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[RealSandboxedWorkflowRunnerProbeAgent],
        activities=[*SANDBOX_ACTIVITIES, agent.tool_activity(real_sandbox_probe)],
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
