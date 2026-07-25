# ABOUTME: Workflow side. ``SandboxHandle`` is what a tool body receives: a stateless object
# holding a provider name plus a serializable ``SandboxState``, whose every method is one Temporal
# activity. So each sandbox operation is independently durable and retried, and the only sandbox
# data in workflow history is the state — not the workspace.
#
# A handle is safe to hold for a whole run and safe across replay, because it holds no live
# connection: the worker-side provider re-attaches from the state whenever it needs to.

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from temporalio import workflow
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.sandbox import _activity_models as m
from temporal_agent_harness.harness.sandbox.protocol import ExecResult, FsEntry, SandboxState

if TYPE_CHECKING:
    from collections.abc import Mapping

# Sandbox operations are I/O against a remote environment: a cold start or a slow install can
# legitimately take minutes, while a stuck one should not hang a turn forever.
DEFAULT_ACTIVITY_CONFIG = ActivityConfig(start_to_close_timeout=timedelta(minutes=5))


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
    ) -> None:
        """Bind ``state`` to the named provider's activities."""
        self._provider = provider
        self._state = state
        self._config: ActivityConfig = config or DEFAULT_ACTIVITY_CONFIG

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
        if not workflow.in_workflow():
            raise RuntimeError(
                "SandboxHandle can only be used inside a workflow (an inline @agent.tool_defn "
                "tool qualifies; an @agent.activity_tool_defn body does not, because its code "
                "runs in an activity where execute_activity is unavailable)"
            )
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


__all__ = ["DEFAULT_ACTIVITY_CONFIG", "SandboxHandle"]
