"""Public-facing provider that pairs a name with a real sandbox client."""

from __future__ import annotations

import inspect
import io
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import LocalSnapshot, RemoteSnapshot
from agents.sandbox.session.sandbox_client import BaseSandboxClient
from agents.sandbox.session.sandbox_session import SandboxSession

from temporalio import activity
from temporal_agent_harness.harness.sandbox._activity_models import (
    CreateSessionArgs,
    DeleteSnapshotArgs,
    ExecArgs,
    HydrateWorkspaceArgs,
    PersistWorkspaceArgs,
    PersistWorkspaceResult,
    PtyExecStartArgs,
    PtyExecUpdateResult,
    PtyWriteStdinArgs,
    ReadArgs,
    ReadResult,
    ResumeSessionArgs,
    RunningArgs,
    RunningResult,
    SessionResult,
    StartArgs,
    StateResult,
    StopArgs,
    WriteArgs,
    _HasState,
)
from temporal_agent_harness.harness.sandbox._activity_models import (
    ExecResult as ExecResultModel,
)
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.sandbox._errors import translate_sandbox_errors


# Providers whose activities this worker process registered, by name. Activity tools that take
# an ``Injected[SandboxSession]`` look their provider up here: they are ordinary tool
# activities, so nothing else connects them to the provider's session cache.
_REGISTERED: dict[str, SandboxClientProvider] = {}

# Sessions a provider keeps live by default. Evicting one only drops this worker's handle; the
# next activity for it resumes the session from the state it carries.
DEFAULT_MAX_CACHED_SESSIONS = 128


class SandboxClientProvider:
    """A named sandbox client provider for Temporal workflows.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Wraps a ``BaseSandboxClient`` with a unique name so that multiple
    sandbox backends can be registered on a single Temporal worker.  Each
    provider gets its own set of Temporal activities whose names are prefixed
    with the provider name, allowing them to coexist on the same task queue.

    On the **worker side**, pass one or more providers to the plugin::

        plugin = OpenAIAgentsPlugin(
            sandbox_clients=[
                SandboxClientProvider("daytona", DaytonaSandboxClient()),
                SandboxClientProvider("local", UnixLocalSandboxClient()),
            ],
        )

    On the **workflow side**, reference a provider by name via
    :func:`temporal_agent_harness.ai_sdks.openai_agents.workflow.temporal_sandbox_client`::

        run_config = RunConfig(
            sandbox=SandboxRunConfig(
                client=temporal_sandbox_client("daytona"),
                ...
            ),
        )

    Any other worker registers the same activities with :func:`sandbox_activities`.

    Args:
        name: A unique name for this sandbox backend (e.g. ``"daytona"``,
            ``"local"``).  Must match the name used on the workflow side.
        client: The real ``BaseSandboxClient`` that performs sandbox
            lifecycle and I/O operations on the worker. Shared runtime objects a
            session needs, such as a ``RemoteSnapshot`` store, are the client's own
            ``dependencies``.
        affinity_task_queue: A task queue only this worker process polls, with the
            same sandbox activities (and ``Injected[SandboxSession]`` tool activities)
            registered on it.
            When set, a session this worker creates or resumes is pinned to it, so
            later calls reach the worker that holds the live session and its PTY
            processes. Without it, any worker may serve any call; sessions are
            resumed from their state on a cache miss, but a ``write_stdin`` to a
            process another worker started fails.
        max_cached_sessions: How many live sessions this worker keeps. The least
            recently used one is dropped beyond that (the sandbox itself is
            untouched; it is resumed from state when next needed).
    """

    def __init__(
        self,
        name: str,
        client: BaseSandboxClient[Any],
        *,
        affinity_task_queue: str | None = None,
        max_cached_sessions: int = DEFAULT_MAX_CACHED_SESSIONS,
    ) -> None:
        """Initialize the provider."""
        self._name = name
        self._client = client
        self._affinity_task_queue = affinity_task_queue
        self._max_cached_sessions = max_cached_sessions
        self._sessions: OrderedDict[str, SandboxSession] = OrderedDict()

    @property
    def name(self) -> str:
        """The provider name used as an activity-name prefix."""
        return self._name

    @property
    def affinity_task_queue(self) -> str | None:
        """This worker's own task queue, if it runs one for sandbox activities."""
        return self._affinity_task_queue

    async def session_for(self, state: SandboxSessionState) -> SandboxSession:
        """The live session for ``state``: cached, or resumed from the state."""
        key = str(state.session_id)
        session = self._sessions.get(key)
        if session is None:
            session = await self._client.resume(state)
            self._cache(session)
        else:
            self._sessions.move_to_end(key)
        return session

    async def _session(self, args: _HasState) -> SandboxSession:
        return await self.session_for(args.state)

    def _cache(self, session: SandboxSession) -> None:
        key = str(session.state.session_id)
        self._sessions[key] = session
        self._sessions.move_to_end(key)
        while len(self._sessions) > self._max_cached_sessions:
            self._sessions.popitem(last=False)

    def _session_result(self, session: SandboxSession) -> SessionResult:
        return SessionResult(
            state=session.state,
            supports_pty=session.supports_pty(),
            task_queue=self._affinity_task_queue,
        )

    def _get_activities(self) -> Sequence[Callable[..., Any]]:
        """Return all activity callables for registration with a Temporal Worker."""
        prefix = self._name
        _REGISTERED[prefix] = self

        # -- Client-level operations (lifecycle) --

        @activity.defn(name=f"{prefix}-sandbox_client_create")
        async def create_session(args: CreateSessionArgs) -> SessionResult:
            with translate_sandbox_errors():
                session = await self._client.create(
                    snapshot=args.snapshot_spec,
                    manifest=args.manifest,
                    options=args.client_options,
                )
                self._cache(session)
                return self._session_result(session)

        @activity.defn(name=f"{prefix}-sandbox_client_resume")
        async def resume_session(args: ResumeSessionArgs) -> SessionResult:
            with translate_sandbox_errors():
                session = await self._client.resume(args.state)
                self._cache(session)
                return self._session_result(session)

        @activity.defn(name=f"{prefix}-sandbox_client_delete")
        async def delete_session(args: StopArgs) -> None:
            with translate_sandbox_errors():
                session = await self._session(args)
                await self._client.delete(session)
                self._sessions.pop(str(args.state.session_id), None)
                return None

        # -- Session-level operations (I/O and lifecycle) --

        @activity.defn(name=f"{prefix}-sandbox_session_exec")
        async def exec_(args: ExecArgs) -> ExecResultModel:
            with translate_sandbox_errors():
                session = await self._session(args)
                result = await session.exec(
                    *args.command,
                    timeout=args.timeout,
                    shell=args.shell,
                    user=args.user,
                )
                return ExecResultModel(
                    stdout=result.stdout,
                    stderr=result.stderr,
                    exit_code=result.exit_code,
                )

        @activity.defn(name=f"{prefix}-sandbox_session_read")
        async def read(args: ReadArgs) -> ReadResult:
            with translate_sandbox_errors():
                session = await self._session(args)
                handle = await session.read(Path(args.path))
                return ReadResult(data=handle.read())

        @activity.defn(name=f"{prefix}-sandbox_session_write")
        async def write(args: WriteArgs) -> None:
            with translate_sandbox_errors():
                session = await self._session(args)
                await session.write(Path(args.path), io.BytesIO(args.data))
                return None

        @activity.defn(name=f"{prefix}-sandbox_session_running")
        async def running(args: RunningArgs) -> RunningResult:
            with translate_sandbox_errors():
                session = await self._session(args)
                return RunningResult(is_running=await session.running())

        @activity.defn(name=f"{prefix}-sandbox_session_persist_workspace")
        async def persist_workspace(
            args: PersistWorkspaceArgs,
        ) -> PersistWorkspaceResult:
            with translate_sandbox_errors():
                session = await self._session(args)
                stream = await session.persist_workspace()
                return PersistWorkspaceResult(data=stream.read())

        @activity.defn(name=f"{prefix}-sandbox_session_hydrate_workspace")
        async def hydrate_workspace(args: HydrateWorkspaceArgs) -> None:
            with translate_sandbox_errors():
                session = await self._session(args)
                await session.hydrate_workspace(io.BytesIO(args.data))
                return None

        @activity.defn(name=f"{prefix}-sandbox_session_pty_exec_start")
        async def pty_exec_start(args: PtyExecStartArgs) -> PtyExecUpdateResult:
            with translate_sandbox_errors():
                session = await self._session(args)
                update = await session.pty_exec_start(
                    *args.command,
                    timeout=args.timeout,
                    shell=args.shell,
                    user=args.user,
                    tty=args.tty,
                    yield_time_s=args.yield_time_s,
                    max_output_tokens=args.max_output_tokens,
                )
                return PtyExecUpdateResult(
                    process_id=update.process_id,
                    output=update.output,
                    exit_code=update.exit_code,
                    original_token_count=update.original_token_count,
                )

        @activity.defn(name=f"{prefix}-sandbox_session_pty_write_stdin")
        async def pty_write_stdin(args: PtyWriteStdinArgs) -> PtyExecUpdateResult:
            with translate_sandbox_errors():
                session = await self._session(args)
                update = await session.pty_write_stdin(
                    session_id=args.session_id,
                    chars=args.chars,
                    yield_time_s=args.yield_time_s,
                    max_output_tokens=args.max_output_tokens,
                )
                return PtyExecUpdateResult(
                    process_id=update.process_id,
                    output=update.output,
                    exit_code=update.exit_code,
                    original_token_count=update.original_token_count,
                )

        @activity.defn(name=f"{prefix}-sandbox_session_start")
        async def start(args: StartArgs) -> StateResult:
            with translate_sandbox_errors():
                session = await self._session(args)
                await session.start()
                return StateResult(state=session.state)

        @activity.defn(name=f"{prefix}-sandbox_session_stop")
        async def session_stop(args: StopArgs) -> StateResult:
            with translate_sandbox_errors():
                session = await self._session(args)
                await session.stop()
                return StateResult(state=session.state)

        @activity.defn(name=f"{prefix}-sandbox_session_shutdown")
        async def session_shutdown(args: StopArgs) -> StateResult:
            # The entry is kept: ``client.delete`` usually follows and needs this same session
            # object, and resuming a shut-down sandbox just to delete it could rebuild it.
            session = self._sessions.get(str(args.state.session_id))
            if session is None:
                return StateResult(state=args.state)
            with translate_sandbox_errors():
                await session.shutdown()
            return StateResult(state=session.state)

        @activity.defn(name=f"{prefix}-sandbox_snapshot_delete")
        async def delete_snapshot(args: DeleteSnapshotArgs) -> None:
            await self._delete_snapshot(args.state)

        return [
            create_session,
            resume_session,
            delete_session,
            exec_,
            read,
            write,
            running,
            persist_workspace,
            hydrate_workspace,
            pty_exec_start,
            pty_write_stdin,
            start,
            session_stop,
            session_shutdown,
            delete_snapshot,
        ]

    async def _delete_snapshot(self, state: SandboxSessionState) -> None:
        """Delete the workspace snapshot ``state`` points at, where its store allows it.

        The OpenAI snapshot API has no delete, so this covers the stores the harness can
        reach: a local snapshot file, and a ``RemoteSnapshot`` whose store (the client
        dependency it names) has an optional ``delete(snapshot_id)``. Anything else is left
        alone, and so is a store without ``delete``.
        """
        snapshot = state.snapshot
        if isinstance(snapshot, LocalSnapshot):
            snapshot._path().unlink(missing_ok=True)
        elif isinstance(snapshot, RemoteSnapshot):
            dependencies = self._client._resolve_dependencies()
            if dependencies is None:
                return
            try:
                store = await dependencies.require(
                    snapshot.client_dependency_key, consumer="snapshot retention"
                )
                delete = getattr(store, "delete", None)
                if callable(delete):
                    result = delete(snapshot.id)
                    if inspect.isawaitable(result):
                        await result
            finally:
                await dependencies.aclose()


def sandbox_activities(
    providers: Sequence[SandboxClientProvider],
) -> list[Callable[..., Any]]:
    """The activities a worker registers to serve agent sandboxes from ``providers``.

    ``AgentHarnessPlugin(sandbox_clients=providers)`` registers these for you. The sandbox
    tools from ``runner.sandbox_tools()`` run in the workflow on these, so they need no
    activities of their own::

        Worker(..., activities=sandbox_activities(
            [SandboxClientProvider("local", UnixLocalSandboxClient())]
        ))
    """
    names = [p.name for p in providers]
    if len(names) != len(set(names)):
        raise ValueError(f"sandbox provider names must be unique, got {names}")
    return [a for p in providers for a in p._get_activities()]


async def resolve_session(provider_name: str, state: Mapping[str, Any]) -> SandboxSession:
    """The live session a sandbox tool activity runs against, from the state its
    ``AgentToolContext`` carries."""
    provider = _REGISTERED.get(provider_name)
    if provider is None:
        raise ApplicationError(
            f"no sandbox provider named {provider_name!r} is registered on this worker; "
            "pass it to AgentHarnessPlugin(sandbox_clients=[...]) (or the OpenAI Agents "
            "plugin's sandbox_clients=)",
            type="SandboxNotRegistered",
            non_retryable=True,
        )
    with translate_sandbox_errors():
        return await provider.session_for(SandboxSessionState.parse(dict(state)))
