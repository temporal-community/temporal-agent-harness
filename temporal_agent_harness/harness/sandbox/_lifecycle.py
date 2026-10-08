"""The workflow-owned lifecycle of an agent's sandbox."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from agents.sandbox import Manifest
from agents.sandbox.capabilities import Capability
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.pty_types import PtyExecUpdate
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.types import ExecResult, User
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from temporal_agent_harness.harness.sandbox._activity_models import DeleteSnapshotArgs
from temporal_agent_harness.harness.sandbox._client import TemporalSandboxClient
from temporal_agent_harness.harness.sandbox._errors import sandbox_error_from
from temporal_agent_harness.harness.sandbox._session import TemporalSandboxSession
from temporal_agent_harness.harness.sandbox.config import SandboxConfig

# ``absent``: nothing created yet. ``idle``: a sandbox exists but is not running (shut down
# by the idle policy, or created and not yet started). ``running``: started and usable.
# ``closed``: torn down for good.
SandboxPhase = Literal["absent", "idle", "running", "closed"]

_T = TypeVar("_T")


class SandboxLifecycle:
    """Creates, idles, resumes and closes one sandbox, from workflow code.

    Only this class changes the sandbox's lifecycle, and only through activities. What it
    keeps is the ``SandboxSessionState`` (inside its :class:`TemporalSandboxSession`) and a
    phase, both rebuilt on replay from activity results.

    ``capabilities`` are this agent's own copies of the configured capabilities, bound to a
    :class:`CapabilitySession`, as OpenAI's runtime binds them for a run. ``manifest`` is the
    configured one after each capability's ``process_manifest``; the sandbox is created
    with it.
    """

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config
        self.client = TemporalSandboxClient(
            config.client, config.activity_config, affinity_timeout=config.affinity_timeout
        )
        self.capabilities: list[Capability] = [c.clone() for c in config.capabilities]
        session = CapabilitySession(self)
        manifest = config.manifest
        for capability in self.capabilities:
            capability.bind(session)
            base = manifest if manifest is not None else Manifest()
            processed = capability.process_manifest(base)
            if processed is not base:
                manifest = processed
        self.manifest = manifest
        self._session: TemporalSandboxSession | None = None
        self._phase: SandboxPhase = "absent"
        # Parallel tool calls in one turn all ask for the sandbox; only one may create it.
        self._lock = asyncio.Lock()
        # Whether the sandbox has been used since the idle policy last ran.
        self._used_since_idle = False
        # Every snapshot this sandbox has written to, by id, for retention on close.
        self._snapshots: dict[str, SandboxSessionState] = {}

    @property
    def phase(self) -> SandboxPhase:
        return self._phase

    @property
    def session(self) -> TemporalSandboxSession | None:
        """The current session, if a sandbox has been created; it may not be running."""
        return self._session

    @property
    def idle_timer_armed(self) -> bool:
        """Whether an idle period should be timed: there is a policy with something to do,
        and the sandbox is running and has been used since the policy last ran."""
        idle = self.config.idle
        return (
            idle is not None
            and idle.action != "keep"
            and self._phase == "running"
            and self._used_since_idle
        )

    async def ensure_running(self) -> TemporalSandboxSession:
        """The running session, creating or resuming the sandbox first if needed."""
        async with self._lock:
            if self._phase == "closed":
                raise ApplicationError(
                    "this agent's sandbox has already been closed",
                    type="SandboxClosed",
                    non_retryable=True,
                )
            if self._phase == "absent":
                self._session = await self.client.create_session(
                    snapshot=self.config.snapshot,
                    manifest=self.manifest,
                    options=self.config.options,
                )
                self._phase = "idle"
                self._remember_snapshot()
                await self._session.start()
            elif self._phase == "idle":
                assert self._session is not None
                self._session = await self.client.resume_session(self._session.state)
                await self._session.start()
            self._phase = "running"
            self._used_since_idle = True
            self._remember_snapshot()
            assert self._session is not None
            return self._session

    async def apply_idle_policy(self) -> None:
        """Apply the idle action once, after the agent has been idle for ``idle.after``."""
        idle = self.config.idle
        async with self._lock:
            if idle is None or self._phase != "running":
                return
            session = self._session
            assert session is not None
            self._used_since_idle = False
            if idle.action in ("persist", "persist_and_shutdown"):
                await session.stop()
                self._remember_snapshot()
            if idle.action in ("persist_and_shutdown", "pause"):
                await session.shutdown()
                self._phase = "idle"

    async def close(self) -> None:
        """Tear the sandbox down: final persist (if the snapshot is kept), shut down, delete,
        then delete its snapshots (unless kept).

        A failed step does not skip the later ones. Failures are logged rather than raised,
        so a backend that refuses to clean up never fails the agent's own completion.
        """
        async with self._lock:
            phase, self._phase = self._phase, "closed"
            session = self._session
            if phase in ("absent", "closed") or session is None:
                return
            first_error: BaseException | None = None

            async def step(name: str, op: Callable[[], Awaitable[Any]]) -> None:
                nonlocal first_error
                try:
                    await op()
                except Exception as e:
                    workflow.logger.warning("sandbox close: %s failed: %s", name, e)
                    first_error = first_error or e

            if phase == "running":
                if self.config.keep_snapshot:
                    await step("stop", session.stop)
                    self._remember_snapshot()
                await step("shutdown", session.shutdown)
            await step("delete", lambda: self.client.delete(session))
            if not self.config.keep_snapshot:
                for state in list(self._snapshots.values()):
                    await step(
                        "snapshot delete",
                        lambda state=state: workflow.execute_activity(
                            f"{self.config.client}-sandbox_snapshot_delete",
                            arg=DeleteSnapshotArgs(state=state),
                            **self.config.activity_config,
                        ),
                    )
            if first_error is not None:
                workflow.logger.warning(
                    "sandbox close finished with errors; first: %s", first_error
                )

    def _remember_snapshot(self) -> None:
        if self._session is not None:
            state = self._session.state
            self._snapshots[state.snapshot.id] = state


class CapabilitySession(BaseSandboxSession):
    """The session an agent's capabilities are bound to, for the agent's whole life.

    Each call goes to the lifecycle's current :class:`TemporalSandboxSession`, creating or
    resuming the sandbox first, so a capability's tools can be built before the sandbox
    exists and keep working across idle shutdowns. A ``SandboxError`` raised on the worker
    is raised here as that same error, so a tool can handle it as it would on a local
    session. Starting and stopping the sandbox is the lifecycle's job, not a capability's.
    """

    def __init__(self, lifecycle: SandboxLifecycle) -> None:
        self._lifecycle = lifecycle

    @property
    def state(self) -> SandboxSessionState:
        """The current session's state. Exists once the sandbox does; a capability tool
        only runs after the sandbox is running."""
        session = self._lifecycle.session
        if session is None:
            raise RuntimeError("the sandbox has not been created yet")
        return session.state

    @state.setter
    def state(self, value: SandboxSessionState) -> None:  # type: ignore[reportIncompatibleVariableOverride]
        session = self._lifecycle.session
        if session is None:
            raise RuntimeError("the sandbox has not been created yet")
        session.state = value

    def supports_pty(self) -> bool:
        """Whether the sandbox supports PTY operations. Before it is created, this assumes
        it does, so ``write_stdin`` is offered; on a backend without PTYs it then fails."""
        session = self._lifecycle.session
        return True if session is None else session.supports_pty()

    async def _call(self, op: Callable[[TemporalSandboxSession], Awaitable[_T]]) -> _T:
        session = await self._lifecycle.ensure_running()
        try:
            return await op(session)
        except ActivityError as e:
            raise (sandbox_error_from(e) or e) from e

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
    ) -> ExecResult:
        return await self._call(
            lambda s: s.exec(*command, timeout=timeout, shell=shell, user=user)
        )

    async def _exec_internal(
        self, *command: str | Path, timeout: float | None = None
    ) -> ExecResult:
        raise NotImplementedError("CapabilitySession overrides exec() directly")

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        return await self._call(lambda s: s.read(path, user=user))

    async def write(self, path: Path, data: io.IOBase, *, user: str | User | None = None) -> None:
        await self._call(lambda s: s.write(path, data, user=user))

    async def running(self) -> bool:
        return self._lifecycle.phase == "running"

    async def persist_workspace(self) -> io.IOBase:
        return await self._call(lambda s: s.persist_workspace())

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        await self._call(lambda s: s.hydrate_workspace(data))

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        return await self._call(
            lambda s: s.pty_exec_start(
                *command,
                timeout=timeout,
                shell=shell,
                user=user,
                tty=tty,
                yield_time_s=yield_time_s,
                max_output_tokens=max_output_tokens,
            )
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        return await self._call(
            lambda s: s.pty_write_stdin(
                session_id=session_id,
                chars=chars,
                yield_time_s=yield_time_s,
                max_output_tokens=max_output_tokens,
            )
        )

    async def start(self) -> None:
        await self._lifecycle.ensure_running()

    async def stop(self) -> None:
        raise RuntimeError("the agent's runner owns this sandbox's lifecycle; it can't be stopped here")

    async def shutdown(self) -> None:
        raise RuntimeError("the agent's runner owns this sandbox's lifecycle; it can't be shut down here")
