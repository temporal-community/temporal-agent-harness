# ABOUTME: Tests for the SDK-neutral sandbox seam (harness/sandbox/*) and the LazyInjection hook
# it is built on. Two layers:
#   * unit — the provider's activity surface, error translation, resume-on-cache-miss, the
#     hydration capability check, and LazyInjection resolution in run_tool;
#   * end-to-end — a real workflow on the time-skipping test server claiming a sandbox lazily,
#     hydrating it, and driving an inline tool that greps the workspace, proving the handle's
#     activity dispatch works and that a sandbox is claimed at most once per run.
#
# The backend under test is an in-memory fake: the point is the seam, not any real sandbox.
#
# Run with: uv run pytest tests/harness/test_sandbox.py -v

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, Mapping

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.harness import AgentWorkflowRunner, agent
from temporal_agent_harness.harness.agent import Injected, LazyInjection
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    TURN_EVENTS_TOPIC,
    AgentConfig,
    AgentEvent,
    AgentEventType,
    AgentMessage,
    AgentMessageReply,
    TextMessage,
    TextReply,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.sandbox import (
    ExecResult,
    FsEntry,
    SandboxHandle,
    SandboxOptions,
    SandboxProvider,
    SandboxReclaimed,
    SandboxRef,
    SandboxState,
    SandboxUnavailable,
    SupportsHydration,
    attach_sandbox,
)
from temporal_agent_harness.harness.sandbox import _activity_models as m

# ---------------------------------------------------------------------------
# A fake backend: in-memory filesystem, scope-keyed claim
# ---------------------------------------------------------------------------


class FakeOptions(SandboxOptions):
    """Claim identity for the fake backend — one sandbox per tenant."""

    tenant_id: str
    fail_with: str | None = None


class FakeBackend:
    """In-memory backend, no hydration capability. Claim is idempotent on ``tenant_id``, mirroring a
    real service that enforces one live sandbox per caller-supplied key."""

    options_model = FakeOptions

    def __init__(self, *, source: Mapping[str, bytes] | None = None) -> None:
        self.source = dict(source or {})
        self.files: dict[str, dict[str, bytes]] = {}
        self.creates = 0
        self.resumes = 0
        self.hydrates = 0
        self.reclaimed: set[str] = set()

    # -- lifecycle --

    async def create(self, options: FakeOptions) -> SandboxState:
        if options.fail_with == "unavailable":
            raise SandboxUnavailable("no capacity")
        self.creates += 1
        # Idempotent: an existing tenant sandbox is returned, not duplicated. A claim after a
        # reclaim yields a LIVE sandbox under the same key — which is what a real claim-or-reuse
        # backend does, and what makes on_reclaim="reacquire" recoverable.
        self.reclaimed.discard(options.tenant_id)
        self.files.setdefault(options.tenant_id, {})
        return SandboxState(
            backend_ref=options.tenant_id,
            supports_pty=True,
            attributes={"tenant_id": options.tenant_id},
        )

    async def resume(self, state: SandboxState) -> SandboxState:
        self.resumes += 1
        if state.backend_ref in self.reclaimed:
            raise SandboxReclaimed(f"sandbox {state.backend_ref} was reclaimed")
        self.files.setdefault(state.backend_ref, {})
        return state

    async def delete(self, state: SandboxState) -> None:
        self.files.pop(state.backend_ref, None)

    # -- I/O --

    def _fs(self, state: SandboxState) -> dict[str, bytes]:
        if state.backend_ref in self.reclaimed:
            raise SandboxReclaimed(f"sandbox {state.backend_ref} was reclaimed")
        return self.files.setdefault(state.backend_ref, {})

    async def exec(
        self, state: SandboxState, command: list[str], timeout: float | None = None
    ) -> ExecResult:
        fs = self._fs(state)
        # Just enough "grep -rn <pattern> ." to exercise a realistic tool.
        if command[:2] == ["grep", "-rn"]:
            pattern = command[2]
            hits = [
                f"{path}:{i + 1}:{line}"
                for path, blob in sorted(fs.items())
                for i, line in enumerate(blob.decode().splitlines())
                if pattern in line
            ]
            return ExecResult(stdout="\n".join(hits), exit_code=0 if hits else 1)
        return ExecResult(stdout=" ".join(command))

    async def run_code(
        self,
        state: SandboxState,
        code: str,
        language: str = "python",
        timeout: float | None = None,
    ) -> ExecResult:
        self._fs(state)
        return ExecResult(stdout=f"{language}:{len(code)}")

    async def read(self, state: SandboxState, path: str) -> bytes:
        return self._fs(state)[path]

    async def write(self, state: SandboxState, files: Mapping[str, bytes]) -> int:
        fs = self._fs(state)
        fs.update(files)
        return sum(len(v) for v in files.values())

    async def ls(self, state: SandboxState, path: str, depth: int = 1) -> list[FsEntry]:
        return [
            FsEntry(path=p, is_dir=False, size=len(b))
            for p, b in sorted(self._fs(state).items())
            if p.startswith(path.rstrip("."))
        ]

    async def running(self, state: SandboxState) -> bool:
        return state.backend_ref not in self.reclaimed


class HydratingBackend(FakeBackend):
    """``FakeBackend`` plus the optional :class:`SupportsHydration` capability.

    Kept as a separate class rather than stubbing the methods out on the base, so the capability
    check under test is genuine attribute absence — not an attribute set to ``None``, whose
    ``isinstance`` behaviour against a runtime-checkable Protocol is a detail we should not lean on.
    """

    async def hydrate(self, state: SandboxState, locator: str | None = None) -> int:
        """Pull from ``source`` — by reference, so no bytes crossed the activity boundary."""
        self.hydrates += 1
        fs = self._fs(state)
        fs.update(self.source)
        return len(self.source)

    async def persist(self, state: SandboxState, locator: str | None = None) -> str:
        self.source.update(self._fs(state))
        return locator or f"fake://{state.backend_ref}"


# ---------------------------------------------------------------------------
# Unit: protocol conformance + provider behaviour
# ---------------------------------------------------------------------------


def test_hydration_is_an_optional_capability() -> None:
    """A backend opts into by-reference data movement; one that does not is still a valid backend."""
    assert isinstance(HydratingBackend(), SupportsHydration)
    assert not isinstance(FakeBackend(), SupportsHydration)


def test_provider_activity_names_are_prefixed() -> None:
    """Two backends coexist on one task queue because every activity carries its provider name."""
    a = SandboxProvider("alpha", FakeBackend())
    b = SandboxProvider("beta", FakeBackend())
    names_a = {act.__temporal_activity_definition.name for act in a.activities()}  # type: ignore[attr-defined]
    names_b = {act.__temporal_activity_definition.name for act in b.activities()}  # type: ignore[attr-defined]
    assert "alpha-sandbox_exec" in names_a
    assert "beta-sandbox_exec" in names_b
    assert names_a.isdisjoint(names_b)


def _activity(provider: SandboxProvider, operation: str) -> Any:
    name = m.activity_name(provider.name, operation)
    for act in provider.activities():
        if act.__temporal_activity_definition.name == name:  # type: ignore[attr-defined]
            return act
    raise AssertionError(f"no activity named {name}")


async def test_create_validates_options_into_the_backend_model() -> None:
    """Options cross as a plain mapping and come back as the backend's own type."""
    backend = FakeBackend()
    provider = SandboxProvider("workspace", backend)
    state = await _activity(provider, m.CREATE)(m.CreateArgs(options={"tenant_id": "acme"}))
    assert state.backend_ref == "acme"
    assert state.supports_pty is True
    assert backend.creates == 1


async def test_operations_resume_once_then_reuse_the_cached_state() -> None:
    """A worker that has not seen a state re-attaches; later calls skip the round trip."""
    backend = FakeBackend()
    provider = SandboxProvider("workspace", backend)
    state = SandboxState(backend_ref="acme")

    await _activity(provider, m.WRITE)(
        m.WriteArgs(state=state, files={"notes.txt": b"hello\nworld\n"})
    )
    assert backend.resumes == 1

    read = await _activity(provider, m.READ)(m.ReadArgs(state=state, path="notes.txt"))
    assert read.data == b"hello\nworld\n"
    assert backend.resumes == 1  # cached, not resumed again


async def test_retryable_error_propagates_unchanged() -> None:
    """A capacity bounce stays retryable so the activity's retry policy is the queue."""
    provider = SandboxProvider("workspace", FakeBackend())
    with pytest.raises(SandboxUnavailable):
        await _activity(provider, m.CREATE)(
            m.CreateArgs(options={"tenant_id": "acme", "fail_with": "unavailable"})
        )


async def test_reclaim_becomes_a_non_retryable_application_error() -> None:
    """A reclaimed sandbox is terminal: retrying would hand the agent an empty workspace."""
    backend = FakeBackend()
    backend.reclaimed.add("acme")
    provider = SandboxProvider("workspace", backend)
    with pytest.raises(ApplicationError) as caught:
        await _activity(provider, m.EXEC)(
            m.ExecArgs(state=SandboxState(backend_ref="acme"), command=["ls"])
        )
    assert caught.value.non_retryable
    assert caught.value.type == "sandbox_reclaimed"


async def test_hydrate_on_a_backend_without_the_capability_fails_clearly() -> None:
    provider = SandboxProvider("workspace", FakeBackend())
    with pytest.raises(ApplicationError) as caught:
        await _activity(provider, m.HYDRATE)(
            m.LocatorArgs(state=SandboxState(backend_ref="acme"))
        )
    assert caught.value.type == "sandbox_hydration_unsupported"
    assert caught.value.non_retryable


async def test_hydrate_moves_data_without_it_crossing_the_boundary() -> None:
    """The activity argument is a locator; the files arrive backend-side."""
    backend = HydratingBackend(source={"chart.md": b"resting heart rate 68\n"})
    provider = SandboxProvider("workspace", backend)
    state = SandboxState(backend_ref="acme")

    result = await _activity(provider, m.HYDRATE)(m.LocatorArgs(state=state, locator=None))

    assert result.files_written == 1
    assert backend.files["acme"]["chart.md"] == b"resting heart rate 68\n"


def test_write_args_round_trip_binary_content() -> None:
    """Bytes survive a JSON payload converter via the base64 wire form."""
    args = m.WriteArgs(state=SandboxState(backend_ref="acme"), files={"b.bin": b"\x00\xff\x01"})
    assert m.WriteArgs.model_validate_json(args.model_dump_json()).files["b.bin"] == b"\x00\xff\x01"


# ---------------------------------------------------------------------------
# Unit: LazyInjection
# ---------------------------------------------------------------------------


def test_sandbox_ref_is_a_lazy_injection() -> None:
    assert isinstance(SandboxRef("workspace"), LazyInjection)
    assert not isinstance("a plain value", LazyInjection)


async def test_unattached_sandbox_ref_explains_itself() -> None:
    class FakeRunner:
        injection_slots: dict[str, Any] = {}

    with pytest.raises(RuntimeError, match="no sandbox attached"):
        await SandboxRef("workspace").resolve_injection(FakeRunner())  # type: ignore[arg-type]


async def test_resolve_injections_only_touches_lazy_values() -> None:
    """Plain injections pass through by identity; lazy ones are awaited."""
    from temporal_agent_harness.harness.agent_workflow import _resolve_injections

    class Counter:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve_injection(self, runner: Any) -> str:
            self.calls += 1
            return "resolved"

    plain: dict[str, Any] = {"a": 1, "b": "two"}
    assert await _resolve_injections(plain, None) is plain  # type: ignore[arg-type]
    assert await _resolve_injections(None, None) is None  # type: ignore[arg-type]

    counter = Counter()
    resolved = await _resolve_injections({"a": 1, "lazy": counter}, None)  # type: ignore[arg-type]
    assert resolved == {"a": 1, "lazy": "resolved"}
    assert counter.calls == 1


async def test_handle_outside_a_workflow_is_rejected() -> None:
    """The handle dispatches activities, so it cannot be used from an activity body."""
    handle = SandboxHandle("workspace", SandboxState(backend_ref="acme"))
    with pytest.raises(RuntimeError, match="only be used inside a workflow"):
        await handle.exec("ls")


# ---------------------------------------------------------------------------
# End-to-end: a workflow claiming, hydrating, and grepping a sandbox
# ---------------------------------------------------------------------------


@agent.tool_defn()
async def grep_workspace(sandbox: Injected[SandboxHandle], pattern: str) -> str:
    """Search the workspace for `pattern`, returning matching lines."""
    result = await sandbox.exec("grep", "-rn", pattern, ".")
    return result.stdout or f"no matches for {pattern!r}"


SANDBOX_INJECTIONS: dict[str, Any] = {"sandbox": SandboxRef("workspace")}

# The backend the worker's activities run against. Module-level so the test can inspect its
# counters after the run — the workflow runs unsandboxed in this process, so this is the same
# object the activities mutate.
BACKEND = HydratingBackend(
    source={
        "chart.md": b"resting heart rate 68\nno known allergies\n",
        "plan.md": b"follow up in 6 weeks\n",
    }
)


@workflow.defn
@agent.defn
class SandboxProbeAgent:
    """A real harness agent whose tool needs a sandbox, driven through run_tool.

    This is the whole delivery path under test: a module-level tool declaring
    ``Injected[SandboxHandle]``, a static injections mapping holding only a ``SandboxRef``, and a
    per-run ``attach_sandbox`` — no SDK involved anywhere.
    """

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        attach_sandbox(self._runner, "workspace", FakeOptions(tenant_id="acme"))

    @workflow.run
    async def run(self, config: AgentConfig) -> None:
        await self._runner.run(self)

    @agent.accepts
    async def probe(self, message: TextMessage) -> TextReply:
        """Grep for the caller's pattern, then for one that cannot match."""
        hit = await self._runner.run_tool(
            "grep-1", grep_workspace, pattern=message.text, injections=SANDBOX_INJECTIONS
        )
        miss = await self._runner.run_tool(
            "grep-2", grep_workspace, pattern="nothing-here", injections=SANDBOX_INJECTIONS
        )
        return TextReply(text=f"{hit}||{miss}")


@pytest_asyncio.fixture
async def client_and_queue() -> Any:
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    provider = SandboxProvider("workspace", BACKEND)
    task_queue = f"sandbox-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[SandboxProbeAgent],
        activities=list(provider.activities()),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def _tool_events(client: Client, workflow_id: str) -> list[Any]:
    stream = WorkflowStreamClient.create(client, workflow_id)
    events: list[Any] = []
    async for item in stream.subscribe(
        topics=[TURN_EVENTS_TOPIC],
        from_offset=0,
        result_type=AgentEvent,
        poll_cooldown=timedelta(milliseconds=10),
    ):
        envelope: AgentEvent = item.data
        events.append(envelope.event)
        if envelope.event.type == AgentEventType.TURN_END:
            break
    return events


async def test_end_to_end_injected_sandbox_tool(client_and_queue: Any) -> None:
    """A sandbox reaches an inline tool with no SDK in the picture, claimed once per run."""
    client, task_queue = client_and_queue
    BACKEND.creates = 0
    BACKEND.hydrates = 0

    handle = await client.start_workflow(
        SandboxProbeAgent.run,
        AgentConfig(),
        id=f"SandboxProbeAgent-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe", payload={"text": "allergies"}, expected_turn=1),
        result_type=AgentMessageReply,
    )

    events = await _tool_events(client, handle.id)
    ends = {
        e.tool_id: e.tool_output
        for e in events
        if e.type == AgentEventType.TOOL_END
    }

    # The tool ran inside the workflow, dispatched sandbox activities, and got real output back.
    assert "chart.md:2:no known allergies" in ends["grep-1"]
    assert ends["grep-2"] == "no matches for 'nothing-here'"

    # Claimed and hydrated exactly once despite two tool calls: the slot memoises per run, so the
    # sandbox is provisioned lazily on first use and reused thereafter.
    assert BACKEND.creates == 1
    assert BACKEND.hydrates == 1

    # The model never sees the injected parameter.
    starts = {e.tool_id: e.tool_input for e in events if e.type == AgentEventType.TOOL_START}
    assert starts["grep-1"] == {"pattern": "allergies"}


# ---------------------------------------------------------------------------
# End-to-end: what happens when the sandbox dies mid-run
# ---------------------------------------------------------------------------
#
# Two policies, two workflows, because the choice is made at attach time. Both kill the sandbox
# between tool calls and record what each subsequent call saw, so the difference is observable
# rather than inferred.

RECLAIM_BACKEND = HydratingBackend(source={"a.md": b"hello\n"})


@agent.tool_defn()
async def touch_workspace(sandbox: Injected[SandboxHandle], label: str) -> str:
    """Read the workspace, tagging the result with `label`."""
    result = await sandbox.exec("grep", "-rn", "hello", ".")
    return f"{label}:{result.stdout}"


TOUCH_INJECTIONS: dict[str, Any] = {"sandbox": SandboxRef("workspace")}


class _ReclaimProbeBase:
    """Runs the tool, kills the sandbox, then runs it twice more, logging every outcome."""

    on_reclaim: Any = "fail"

    @workflow.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.dangerously_skip_all(),
        )
        attach_sandbox(
            self._runner,
            "workspace",
            FakeOptions(tenant_id="acme"),
            on_reclaim=self.on_reclaim,
        )
        self._log: list[str] = []

    @workflow.query
    def log(self) -> list[str]:
        return self._log

    @agent.accepts
    async def probe(self, message: TextMessage) -> TextReply:
        """Touch the workspace, lose the sandbox, then touch it twice more."""
        await self._call("first")
        # The sandbox goes away underneath the run: TTL, node drain, backend GC.
        RECLAIM_BACKEND.reclaimed.add("acme")
        await self._call("second")
        await self._call("third")
        self._log.append(f"creates={RECLAIM_BACKEND.creates}")
        return TextReply(text="done")

    async def _call(self, label: str) -> None:
        try:
            self._log.append(
                await self._runner.run_tool(
                    label, touch_workspace, label=label, injections=TOUCH_INJECTIONS
                )
            )
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            self._log.append(f"{label}:FAILED:{type(exc).__name__}")


@workflow.defn(name="ReclaimFailProbe")
@agent.defn
class ReclaimFailProbe(_ReclaimProbeBase):
    """Default policy: a reclaim is terminal and the model is told."""

    on_reclaim = "fail"

    # Temporal requires @workflow.run on the concrete class, so each probe declares its own.
    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)


@workflow.defn(name="ReclaimReacquireProbe")
@agent.defn
class ReclaimReacquireProbe(_ReclaimProbeBase):
    """Opt-in recovery: the workspace is derived, so a replacement is equivalent."""

    on_reclaim = "reacquire"

    @workflow.run
    async def run(self, _config: AgentConfig) -> None:
        await self._runner.run(self)


async def _run_probe(client: Client, task_queue: str, workflow_cls: Any) -> list[str]:
    handle = await client.start_workflow(
        workflow_cls.run,
        AgentConfig(),
        id=f"{workflow_cls.__name__}-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.execute_update(
        SEND_AGENT_MESSAGE_UPDATE,
        AgentMessage(type="probe", payload={"text": "go"}, expected_turn=1),
        result_type=AgentMessageReply,
    )
    # The turn runs asynchronously after the update returns; wait for its final log line.
    for _ in range(100):
        log: list[str] = await handle.query(workflow_cls.log)
        if any(entry.startswith("creates=") for entry in log):
            return log
        await asyncio.sleep(0.05)
    raise AssertionError(f"probe never finished: {log}")


@pytest_asyncio.fixture
async def reclaim_queue() -> Any:
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    )
    provider = SandboxProvider("workspace", RECLAIM_BACKEND)
    task_queue = f"reclaim-test-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[ReclaimFailProbe, ReclaimReacquireProbe],
        activities=list(provider.activities()),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


def _reset_reclaim_backend() -> None:
    RECLAIM_BACKEND.reclaimed.clear()
    RECLAIM_BACKEND.files.clear()
    RECLAIM_BACKEND.creates = 0
    RECLAIM_BACKEND.hydrates = 0


async def test_reclaim_default_fails_every_later_call(reclaim_queue: Any) -> None:
    """Under ``on_reclaim="fail"`` the sandbox is NOT replaced — by design, not by omission.

    Silently swapping in an empty workspace would present lost work as success, so the failure is
    surfaced to the tool and reaches the model as an error result. The run continues; its sandbox
    does not come back.
    """
    client, task_queue = reclaim_queue
    _reset_reclaim_backend()

    log = await _run_probe(client, task_queue, ReclaimFailProbe)

    assert log[0] == "first:a.md:1:hello"
    assert log[1].startswith("second:FAILED:")
    assert log[2].startswith("third:FAILED:")
    # One claim for the whole run: no replacement was provisioned.
    assert log[3] == "creates=1"


async def test_reclaim_reacquire_recovers_transparently(reclaim_queue: Any) -> None:
    """Under ``on_reclaim="reacquire"`` the sandbox is replaced, re-hydrated, and the call retried.

    The tool never sees the failure, so neither does the model — which is only the right behaviour
    because this workspace is rebuilt entirely from the hydration source.
    """
    client, task_queue = reclaim_queue
    _reset_reclaim_backend()

    log = await _run_probe(client, task_queue, ReclaimReacquireProbe)

    assert log[0] == "first:a.md:1:hello"
    # The reclaim is absorbed: the retry lands on a fresh, hydrated sandbox and returns real output.
    assert log[1] == "second:a.md:1:hello"
    assert log[2] == "third:a.md:1:hello"
    # Exactly one replacement — recovery happened once, then the new sandbox was reused.
    assert log[3] == "creates=2"
    assert RECLAIM_BACKEND.hydrates == 2
