# ABOUTME: Worker side. ``SandboxProvider`` pairs a name with a live ``SandboxBackend`` and emits
# that backend's Temporal activities, each prefixed with the provider name so several backends can
# share one task queue. Register the activities on your Worker; the workflow side then addresses a
# backend by name only (see handle.py) and never imports the backend.
#
# Usage::
#
#     provider = SandboxProvider("workspace", MyBackend())
#     Worker(client, task_queue="agents", activities=[*provider.activities(), ...])

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.sandbox import _activity_models as m
from temporal_agent_harness.harness.sandbox.protocol import (
    ExecResult,
    SandboxBackend,
    SandboxError,
    SandboxState,
    SupportsHydration,
)


def _translate(exc: SandboxError) -> BaseException:
    """Map a backend error onto the right Temporal failure.

    Temporal retries every activity exception by default, so only an error the backend has
    classified as terminal becomes a non-retryable ``ApplicationError``. Everything else propagates
    unchanged and the activity's retry policy backs off — which is the behaviour you want for a
    cold start, a busy backend, or a quota bounce.
    """
    if exc.retryable:
        return exc
    return ApplicationError(str(exc), type=exc.code, non_retryable=True)


class SandboxProvider:
    """A named sandbox backend, exposed to workflows as a set of prefixed activities.

    The provider owns the only live reference to the backend and keeps a small cache of states it
    has already resumed, so a run's second and subsequent operations skip the re-attach round trip.
    On a cache miss — a fresh worker, a restart, a redeploy — it calls ``backend.resume`` with the
    state the workflow carried, which is what makes a sandbox survive losing the worker that
    created it.

    Args:
        name: Unique name for this backend. Prefixes every activity, and is what the workflow side
            passes to :class:`~temporal_agent_harness.harness.sandbox.handle.SandboxHandle`.
        backend: The :class:`~...protocol.SandboxBackend` implementation doing real I/O.
    """

    def __init__(self, name: str, backend: SandboxBackend) -> None:
        """Pair ``name`` with ``backend``."""
        self._name = name
        self._backend = backend
        self._resumed: dict[str, SandboxState] = {}

    @property
    def name(self) -> str:
        """The provider name used as an activity-name prefix."""
        return self._name

    @property
    def backend(self) -> SandboxBackend:
        """The wrapped backend."""
        return self._backend

    async def _live(self, state: SandboxState) -> SandboxState:
        """Return a state this worker has attached to, resuming on first sight."""
        cached = self._resumed.get(state.backend_ref)
        if cached is not None:
            return cached
        try:
            resumed = await self._backend.resume(state)
        except SandboxError as exc:
            raise _translate(exc) from exc
        self._resumed[resumed.backend_ref] = resumed
        return resumed

    def _hydration_backend(self) -> SupportsHydration:
        """The backend as a hydration-capable one, or a terminal failure explaining it is not."""
        if not isinstance(self._backend, SupportsHydration):
            raise ApplicationError(
                f"sandbox backend {self._name!r} does not support hydrate/persist by reference; "
                f"implement SupportsHydration on it, or move data with read()/write()",
                type="sandbox_hydration_unsupported",
                non_retryable=True,
            )
        return self._backend

    def activities(self) -> Sequence[Callable[..., Any]]:
        """Every activity callable for this provider, ready to register on a Worker."""
        prefix = self._name
        backend = self._backend

        @activity.defn(name=m.activity_name(prefix, m.CREATE))
        async def create(args: m.CreateArgs) -> SandboxState:
            options = backend.options_model.model_validate(args.options)
            try:
                state = await backend.create(options)
            except SandboxError as exc:
                raise _translate(exc) from exc
            self._resumed[state.backend_ref] = state
            return state

        @activity.defn(name=m.activity_name(prefix, m.RESUME))
        async def resume(args: m.StateArgs) -> SandboxState:
            return await self._live(args.state)

        @activity.defn(name=m.activity_name(prefix, m.DELETE))
        async def delete(args: m.StateArgs) -> None:
            try:
                await backend.delete(args.state)
            except SandboxError as exc:
                raise _translate(exc) from exc
            finally:
                # Drop the cache entry either way: on success the sandbox is gone, and on failure
                # a retry should re-attach rather than trust a possibly-dead cached state.
                self._resumed.pop(args.state.backend_ref, None)

        @activity.defn(name=m.activity_name(prefix, m.EXEC))
        async def exec_(args: m.ExecArgs) -> ExecResult:
            state = await self._live(args.state)
            try:
                return await backend.exec(state, args.command, args.timeout)
            except SandboxError as exc:
                raise _translate(exc) from exc

        @activity.defn(name=m.activity_name(prefix, m.RUN_CODE))
        async def run_code(args: m.RunCodeArgs) -> ExecResult:
            state = await self._live(args.state)
            try:
                return await backend.run_code(state, args.code, args.language, args.timeout)
            except SandboxError as exc:
                raise _translate(exc) from exc

        @activity.defn(name=m.activity_name(prefix, m.READ))
        async def read(args: m.ReadArgs) -> m.ReadResult:
            state = await self._live(args.state)
            try:
                return m.ReadResult(data=await backend.read(state, args.path))
            except SandboxError as exc:
                raise _translate(exc) from exc

        @activity.defn(name=m.activity_name(prefix, m.WRITE))
        async def write(args: m.WriteArgs) -> m.WriteResult:
            state = await self._live(args.state)
            try:
                return m.WriteResult(bytes_written=await backend.write(state, args.files))
            except SandboxError as exc:
                raise _translate(exc) from exc

        @activity.defn(name=m.activity_name(prefix, m.LS))
        async def ls(args: m.LsArgs) -> m.LsResult:
            state = await self._live(args.state)
            try:
                return m.LsResult(entries=await backend.ls(state, args.path, args.depth))
            except SandboxError as exc:
                raise _translate(exc) from exc

        @activity.defn(name=m.activity_name(prefix, m.RUNNING))
        async def running(args: m.StateArgs) -> m.RunningResult:
            try:
                return m.RunningResult(is_running=await backend.running(args.state))
            except SandboxError as exc:
                raise _translate(exc) from exc

        @activity.defn(name=m.activity_name(prefix, m.HYDRATE))
        async def hydrate(args: m.LocatorArgs) -> m.HydrateResult:
            hydratable = self._hydration_backend()
            state = await self._live(args.state)
            try:
                written = await hydratable.hydrate(state, args.locator)
            except SandboxError as exc:
                raise _translate(exc) from exc
            return m.HydrateResult(files_written=written)

        @activity.defn(name=m.activity_name(prefix, m.PERSIST))
        async def persist(args: m.LocatorArgs) -> m.PersistResult:
            hydratable = self._hydration_backend()
            state = await self._live(args.state)
            try:
                locator = await hydratable.persist(state, args.locator)
            except SandboxError as exc:
                raise _translate(exc) from exc
            return m.PersistResult(locator=locator)

        return [
            create,
            resume,
            delete,
            exec_,
            run_code,
            read,
            write,
            ls,
            running,
            hydrate,
            persist,
        ]


__all__ = ["SandboxProvider"]
