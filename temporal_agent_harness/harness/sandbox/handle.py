# ABOUTME: Workflow side. ``SandboxHandle`` is what a tool body receives: a stateless object
# holding a provider name plus a serializable ``SandboxState``, whose every method is one Temporal
# activity. So each sandbox operation is independently durable and retried, and the only sandbox
# data in workflow history is the state — not the workspace.
#
# A handle is safe to hold for a whole run and safe across replay, because it holds no live
# connection: the worker-side provider re-attaches from the state whenever it needs to.

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.sandbox import _activity_models as m
from temporal_agent_harness.harness.sandbox.protocol import (
    ExecResult,
    FsEntry,
    SandboxReclaimed,
    SandboxState,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

# Sandbox operations are I/O against a remote environment: a cold start or a slow install can
# legitimately take minutes, while a stuck one should not hang a turn forever.
DEFAULT_ACTIVITY_CONFIG = ActivityConfig(start_to_close_timeout=timedelta(minutes=5))


@runtime_checkable
class ReclaimRecovery(Protocol):
    """Supplies a replacement sandbox when the one in use has been reclaimed.

    Implemented by whatever owns the run's sandbox (the slot behind ``attach_sandbox``), because
    recovery means *replacing* the sandbox — which the handle cannot do on its own, since it holds
    only an identity.
    """

    async def recover_from_reclaim(self) -> SandboxState | None:
        """A fresh sandbox's state to retry against, or ``None`` to let the failure stand."""
        ...


def _is_reclaimed(exc: BaseException) -> bool:
    """Whether ``exc`` is (or wraps) a reclaim failure.

    A reclaim raised in a backend surfaces here as an ``ActivityError`` wrapping the
    non-retryable ``ApplicationError`` the provider translated it into, so the whole cause chain
    has to be walked rather than just the outermost exception.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ApplicationError) and current.type == SandboxReclaimed.code:
            return True
        current = getattr(current, "cause", None) or current.__cause__
    return False


class SandboxHandle:
    """A durable handle to one sandbox, usable from workflow code and from inline tools.

    Every method dispatches a Temporal activity, so a handle can only be used where
    ``workflow.execute_activity`` is legal — inside the workflow, which includes the body of a
    :func:`~temporal_agent_harness.harness.agent.tool_defn` (inline) tool. Sandbox tools are
    therefore inline tools: the durability comes from each sandbox operation being its own
    activity, not from wrapping the tool itself in one. Passing a handle into an
    ``activity_tool_defn`` body will not work, and says so when tried.
    """

    def __init__(
        self,
        provider: str,
        state: SandboxState,
        config: ActivityConfig | None = None,
        recovery: ReclaimRecovery | None = None,
    ) -> None:
        """Bind ``state`` to the named provider's activities.

        ``recovery``, when supplied, is consulted if an operation fails because the sandbox was
        reclaimed — see :meth:`_call`.
        """
        self._provider = provider
        self._state = state
        self._config: ActivityConfig = config or DEFAULT_ACTIVITY_CONFIG
        self._recovery = recovery

    def __repr__(self) -> str:
        return f"SandboxHandle(provider={self._provider!r}, backend_ref={self._state.backend_ref!r})"

    @property
    def provider(self) -> str:
        """Name of the provider backing this handle."""
        return self._provider

    @property
    def state(self) -> SandboxState:
        """The sandbox's serializable identity."""
        return self._state

    @property
    def supports_pty(self) -> bool:
        """Whether the backend reported PTY support for this sandbox."""
        return self._state.supports_pty

    async def _call(self, operation: str, arg: object, result_type: type | None = None) -> object:
        """Dispatch one sandbox operation, recovering once if the sandbox was reclaimed.

        Recovery is opt-in and delegated: the handle asks its :class:`ReclaimRecovery` for a
        replacement sandbox, and only retries if it gets one. With no recovery configured — the
        default — a reclaim propagates, which is the right outcome when the workspace held state
        that cannot be rebuilt: silently swapping in an empty sandbox would hide real data loss.

        Exactly one retry, against the replacement's state. If that fails too, the failure stands;
        a reclaim loop means something is wrong with provisioning, not with this call.
        """
        if not workflow.in_workflow():
            raise RuntimeError(
                "SandboxHandle can only be used inside a workflow (an inline @agent.tool_defn "
                "tool qualifies; an @agent.activity_tool_defn body does not, because its code "
                "runs in an activity where execute_activity is unavailable)"
            )
        try:
            return await self._dispatch(operation, arg, result_type)
        except Exception as exc:
            if self._recovery is None or not _is_reclaimed(exc):
                raise
            replacement = await self._recovery.recover_from_reclaim()
            if replacement is None:
                raise
            self._state = replacement
            # Every argument model carries the sandbox identity, so the retry has to travel with
            # the replacement's rather than the reclaimed one's.
            retry_arg = arg.model_copy(update={"state": replacement}) if hasattr(arg, "model_copy") else arg
            return await self._dispatch(operation, retry_arg, result_type)

    async def _dispatch(self, operation: str, arg: object, result_type: type | None) -> object:
        return await workflow.execute_activity(
            m.activity_name(self._provider, operation),
            arg,
            result_type=result_type,
            **self._config,
        )

    # -- execution ----------------------------------------------------------

    async def exec(self, *command: str, timeout: float | None = None) -> ExecResult:
        """Run a command in the sandbox."""
        result = await self._call(
            m.EXEC,
            m.ExecArgs(state=self._state, command=list(command), timeout=timeout),
            ExecResult,
        )
        return result  # type: ignore[return-value]

    async def run_code(
        self,
        code: str,
        *,
        language: str = "python",
        timeout: float | None = None,
    ) -> ExecResult:
        """Execute a code blob in the sandbox."""
        result = await self._call(
            m.RUN_CODE,
            m.RunCodeArgs(state=self._state, code=code, language=language, timeout=timeout),
            ExecResult,
        )
        return result  # type: ignore[return-value]

    # -- filesystem ---------------------------------------------------------

    async def read(self, path: str) -> bytes:
        """Read one file's raw bytes."""
        result = await self._call(m.READ, m.ReadArgs(state=self._state, path=path), m.ReadResult)
        return result.data  # type: ignore[union-attr]

    async def read_text(self, path: str, *, encoding: str = "utf-8") -> str:
        """Read one file and decode it."""
        return (await self.read(path)).decode(encoding)

    async def write(self, files: Mapping[str, bytes]) -> int:
        """Write ``{path: bytes}`` into the sandbox; return bytes written."""
        result = await self._call(
            m.WRITE,
            m.WriteArgs(state=self._state, files=dict(files)),
            m.WriteResult,
        )
        return result.bytes_written  # type: ignore[union-attr]

    async def write_text(
        self,
        files: Mapping[str, str],
        *,
        encoding: str = "utf-8",
    ) -> int:
        """Write ``{path: text}`` into the sandbox; return bytes written."""
        return await self.write({p: t.encode(encoding) for p, t in files.items()})

    async def ls(self, path: str = ".", *, depth: int = 1) -> list[FsEntry]:
        """List entries under ``path``."""
        result = await self._call(
            m.LS,
            m.LsArgs(state=self._state, path=path, depth=depth),
            m.LsResult,
        )
        return result.entries  # type: ignore[union-attr]

    # -- lifecycle ----------------------------------------------------------

    async def running(self) -> bool:
        """Whether the sandbox is currently usable."""
        result = await self._call(
            m.RUNNING, m.StateArgs(state=self._state), m.RunningResult
        )
        return result.is_running  # type: ignore[union-attr]

    async def delete(self) -> None:
        """Tear the sandbox down."""
        await self._call(m.DELETE, m.StateArgs(state=self._state))

    # -- by-reference data movement (backends implementing SupportsHydration) --

    async def hydrate(self, locator: str | None = None) -> int:
        """Populate the sandbox from ``locator``, backend-side; return files written.

        The data never becomes an activity payload — only the locator does. ``locator=None`` asks
        the backend to derive the source from the sandbox's own identity. Fails as a terminal error
        if the backend does not implement ``SupportsHydration``.
        """
        result = await self._call(
            m.HYDRATE,
            m.LocatorArgs(state=self._state, locator=locator),
            m.HydrateResult,
        )
        return result.files_written  # type: ignore[union-attr]

    async def persist(self, locator: str | None = None) -> str:
        """Check the workspace out to ``locator``, backend-side; return a reference to it.

        Use this when the workspace holds state you could not rebuild — a sandbox is compute, and
        losing it loses its filesystem. Call it on a cadence rather than only at the end of a run,
        since a pod that dies unexpectedly never reaches your teardown path.
        """
        result = await self._call(
            m.PERSIST,
            m.LocatorArgs(state=self._state, locator=locator),
            m.PersistResult,
        )
        return result.locator  # type: ignore[union-attr]


__all__ = ["DEFAULT_ACTIVITY_CONFIG", "ReclaimRecovery", "SandboxHandle"]
