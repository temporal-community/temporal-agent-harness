# ABOUTME: Worker-side activities backing sandboxed activity tools: activate (create-or-resume)
# at turn start, pause between turns, unconditional terminate on workflow shutdown. Also the
# shared session registry (`get_or_resume_session`) that `agent_workflow.py`'s sandboxed
# `activity_body` branch reaches into to run a tool call inside the same live sandbox.
#
# The three live on a class (`SandboxActivities`, registered via `sandbox_activities(...)`) purely
# so a worker can inject the `name -> BackendProvider` map an agent selects from by passing a name
# as its `SandboxConfig.backend`: a provider is a live async callable, so it can't be built in
# workflow code or serialized into an activity input — the workflow sends a name, the worker holds
# the callables.
#
# Durability: a live `remote.RemoteSession` handle cannot cross a process boundary, so this
# module pairs a worker-process-local cache (fast path, keyed by workflow run id — stable and
# always available via `activity.info()`, unlike remote-box's own `SessionRef.sandbox_id`, which
# is None for the Subprocess backend) with remote-box's own serializable `SessionRef` (persisted
# as workflow state by the runner, threaded through every call): on a cache miss — a restarted
# worker, or a different worker in a multi-worker deployment picking up later activities for the
# same run — `RemoteSession.resume(ref, ...)` reattaches instead of orphaning the sandbox. Mirrors
# `ai_sdks/openai_agents/sandbox/_sandbox_client_provider.py`'s `_session()` resume-on-miss
# fallback exactly, just built on `remote.RemoteSession` instead of the OpenAI-agents sandbox SDK.
#
# Worker-side only — never import this module from workflow code (that's exactly why the request/
# result models + activity name constants live in the separate, dependency-free `.models`: so
# `agent_workflow.py` can dispatch these activities BY NAME without dragging remote-box, and the
# Daytona/E2B SDKs it depends on, into the deterministic workflow's own execution context. Mirrors
# `harness/code_mode/activities.py`'s split from `batch_models.py` exactly.

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

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

_INSTALL_MESSAGE = (
    "Sandboxed tool support requires the optional `sandbox` extra (remote-box, which in turn "
    "requires Python >= 3.12). Install it with `uv sync --extra sandbox` or "
    "`pip install 'temporal-agent-harness[sandbox]'`."
)

try:
    import asyncio

    from pydantic import TypeAdapter
    from remote import AnyBackendConfig, MissingImageError, RemoteSession, SessionRef, remote
    from remote.backends import BackendConfig, BackendType
    from remote.runtime import ensure_built_once
except ImportError as exc:
    raise RuntimeError(_INSTALL_MESSAGE) from exc

from temporal_agent_harness.harness.sandbox.config import BackendProvider

_BACKEND_ADAPTER: TypeAdapter[AnyBackendConfig] = TypeAdapter(AnyBackendConfig)


def backend_from_dict(data: dict) -> AnyBackendConfig:
    """Reconstruct a typed ``Subprocess``/``E2B``/``Daytona`` config from its plain dict dump.

    Both the lifecycle activities' own inputs (``SandboxActivateInput.backend`` etc.) and
    ``AgentToolContext.sandbox.backend`` carry the backend config as a JSON-compatible dict, not
    remote-box's typed ``AnyBackendConfig`` — so neither core ``agent_workflow.py`` nor the
    dependency-free ``.models`` module needs remote-box installed just to hold that value. This is
    the one place either gets converted back before touching remote-box's actual APIs.

    ``BackendType`` is a plain (int-valued) ``Enum``, and ``AnyBackendConfig``'s discriminator
    field is typed ``Literal[BackendType.X]`` — pydantic's literal check wants the enum MEMBER,
    not the bare int ``model_dump(mode="json")`` produces, and (unlike a plain ``BackendType``
    field) doesn't coerce one back to the other on validation. So ``type`` is coerced explicitly
    before validating the rest.
    """
    fixed = {**data, "type": BackendType(data["type"])}
    return _BACKEND_ADAPTER.validate_python(fixed)


def run_tool_in_sandbox(user_fn, local_project_root: Path, backend: AnyBackendConfig):
    """The ``remote()``-wrapped version of a sandboxed tool's raw function.

    A thin, explicitly-named re-export of remote-box's own ``remote()`` decorator so
    ``agent_workflow.py`` never needs ``from remote import remote`` shadowing its own local names.
    Applying it has no side effects beyond registering the build target (no network I/O; see
    remote-box's own docs) — cheap enough to call fresh on every sandboxed tool invocation, since
    the concrete ``backend``/``local_project_root`` aren't known until the agent (not the tool)
    supplies them at dispatch time.
    """
    return remote(local_project_root=local_project_root, backend=backend)(user_fn)


# Worker-process-local cache of live sandboxes, keyed by workflow run id (stable and unique per
# run, always available via `activity.info()` — unlike `SessionRef.sandbox_id`, which is None for
# the Subprocess backend, so keying on it directly would collide across concurrent runs).
_SESSIONS: dict[str, RemoteSession] = {}


def _to_session_ref(ref: SandboxRef | None) -> SessionRef | None:
    if ref is None:
        return None
    return SessionRef(backend=ref.backend, sandbox_id=ref.sandbox_id)


def _from_session_ref(ref: SessionRef) -> SandboxRef:
    return SandboxRef(backend=ref.backend, sandbox_id=ref.sandbox_id)


async def get_or_resume_session(
    ref: SandboxRef | None, backend: AnyBackendConfig, local_project_root: Path
) -> RemoteSession:
    """The live ``RemoteSession`` for the current activity's workflow run — cache hit, or a
    fresh/resumed session on a miss.

    Shared by the lifecycle activities below AND (via a deferred import) by
    ``agent_workflow.py``'s sandboxed ``activity_body`` branch, so a tool call and the lifecycle
    activities that activate/pause/terminate its sandbox always agree on the one live session for
    this run.
    """
    key = activity.info().workflow_run_id
    cached = _SESSIONS.get(key)
    if cached is not None:
        return cached
    session_ref = _to_session_ref(ref)
    session = (
        RemoteSession(backend=backend, local_project_root=local_project_root)
        if session_ref is None
        else await RemoteSession.resume(
            session_ref, backend=backend, local_project_root=local_project_root
        )
    )
    _SESSIONS[key] = session
    return session


class SandboxActivities:
    """The three sandbox lifecycle activities, holding the worker's backend-provider registry.

    A class rather than three module-level functions for exactly one reason: a
    :data:`~temporal_agent_harness.harness.sandbox.config.BackendProvider` is a live async callable
    that can neither be constructed in workflow code nor serialized into an activity's input, so an
    agent names one as its ``SandboxConfig.backend`` and the worker supplies the callables.
    Constructor injection is how they get in — the map is ordinary worker-side state, and
    ``sandbox_activate`` reads it off ``self``::

        Worker(..., activities=[
            *sandbox_activities({"minted-token": daytona_with_minted_token}),
            agent.tool_activity(my_sandboxed_tool),
        ])

    Everything else stays exactly as tool- and agent-agnostic as before: the activity NAMES are
    unchanged (``sandbox_activate``/``sandbox_pause``/``sandbox_terminate``, the constants in
    ``.models`` the workflow dispatches by), sessions are still keyed by workflow run id in the
    module-level ``_SESSIONS`` cache — shared with the sandboxed-tool dispatch path, which is not a
    method here — and one instance per worker serves every agent on it. Register at most one
    instance per worker: the three names can only be claimed once.
    """

    def __init__(self, backend_providers: Mapping[str, BackendProvider] | None = None) -> None:
        # Copied, not aliased: the registry is read on every first activation for the life of the
        # worker, and a caller mutating the dict it passed in afterwards would silently change
        # which provider an already-running agent resolves.
        self._backend_providers: dict[str, BackendProvider] = dict(backend_providers or {})

    async def _resolve_backend(self, provider_name: str) -> AnyBackendConfig:
        """Look up ``provider_name`` in this worker's registry, await it, and return the backend
        config it produced.

        Called from :meth:`sandbox_activate` only, and only when the agent named a provider instead
        of declaring a config — see ``SandboxConfig.backend`` for the full contract. The provider's
        own failures propagate as-is (a token service being briefly down is exactly what activity
        retries are for); a name this worker doesn't know, or a result it couldn't use, is a
        deployment/authoring error no retry can fix, so those fail non-retryably instead.
        """
        provider = self._backend_providers.get(provider_name)
        if provider is None:
            registered = ", ".join(sorted(self._backend_providers)) or "(none)"
            raise ApplicationError(
                f"no sandbox backend provider named {provider_name!r} is registered on this "
                f"worker; registered names: {registered}. An agent naming it as its "
                "SandboxConfig.backend needs it registered — sandbox_activities({'"
                f"{provider_name}"
                "': provider}) — on EVERY worker serving that agent's sandbox activities.",
                type="SandboxBackendProviderNotRegistered",
                non_retryable=True,
            )
        resolved = await provider()
        if not isinstance(resolved, BackendConfig):
            raise ApplicationError(
                f"sandbox backend provider {provider_name!r} returned {resolved!r}; it must return "
                "a remote-box backend config (Subprocess/E2B/Daytona), not a dump of one",
                type="SandboxBackendProviderInvalidResult",
                non_retryable=True,
            )
        return resolved

    @activity.defn(name=SANDBOX_ACTIVATE_ACTIVITY)
    async def sandbox_activate(self, input: SandboxActivateInput) -> SandboxRefResult:
        """Create-or-resume this run's sandbox — the turn-start hook.

        If ``require_prebuilt`` (mirrors ``SandboxConfig.require_prebuilt``), explicitly refuses to
        build a missing image regardless of ``REMOTE_BOX_AUTO_BUILD``/the backend config's own
        ``auto_build_override`` — a harness-enforced guarantee that production runtime never
        performs an implicit build. Build ahead of time with ``sandbox.build_sandbox()`` from CI.

        When ``input.backend`` is a provider NAME rather than a config (the run's first activation,
        for an agent whose ``SandboxConfig.backend`` names one), the provider is awaited here first,
        and the config it produces is both used to create the sandbox and echoed back in
        ``SandboxRefResult.backend`` for the runner to persist and reuse for the rest of the run.
        """
        resolved_backend: dict[str, Any] | None = None
        if isinstance(input.backend, str):
            backend = await self._resolve_backend(input.backend)
            resolved_backend = backend.model_dump(mode="json")
        else:
            backend = backend_from_dict(input.backend)
        local_project_root = Path(input.local_project_root)
        if input.require_prebuilt:
            try:
                await asyncio.to_thread(
                    ensure_built_once,
                    backend,
                    local_project_root,
                    allow_build=False,
                )
            except MissingImageError as e:
                raise ApplicationError(
                    f"Sandbox image not built: {e}. Build it offline first with "
                    "temporal_agent_harness.harness.sandbox.build_sandbox(config) — run from CI, "
                    "never at runtime.",
                    type="SandboxImageNotBuilt",
                    non_retryable=True,
                ) from e
        session = await get_or_resume_session(input.ref, backend, local_project_root)
        # `.start()` first, unconditionally: idempotent (a no-op if this session already has a
        # handle, whether freshly resumed via get_or_resume_session or reused from the worker-local
        # cache), and guarantees a handle is set before the `async with` below. That matters because
        # `RemoteSession.__aenter__`/`__aexit__` decide "do I own this session" (and thus whether
        # exiting CLOSES it) purely from whether it already had a handle at enter time — entering
        # `async with` on a session we JUST bare-constructed (handle still None) would set owns=True
        # and destroy the sandbox we're trying to activate the instant this block exits. Calling
        # `.start()` first sidesteps that entirely, using only RemoteSession's public API.
        await session.start()
        async with session:  # owns=False now — resumes-if-paused, does NOT close on exit
            pass
        return SandboxRefResult(ref=_from_session_ref(session.ref), backend=resolved_backend)

    @activity.defn(name=SANDBOX_PAUSE_ACTIVITY)
    async def sandbox_pause(self, input: SandboxPauseInput) -> SandboxRefResult:
        """Pause this run's sandbox — the between-turns hook, while idly waiting for the next
        message."""
        backend = backend_from_dict(input.backend)
        local_project_root = Path(input.local_project_root)
        session = await get_or_resume_session(input.ref, backend, local_project_root)
        new_ref = await session.pause()
        return SandboxRefResult(ref=_from_session_ref(new_ref))

    @activity.defn(name=SANDBOX_TERMINATE_ACTIVITY)
    async def sandbox_terminate(self, input: SandboxTerminateInput) -> None:
        """Unconditionally tear down this run's sandbox — called from every reachable termination
        path of ``AgentWorkflowRunner.run()``'s outer ``finally``.

        Best-effort: the remote sandbox may already be gone (e.g. expired via the backend's own
        TTL, the belt-and-suspenders mitigation for the hard-``TERMINATE`` gap this can't otherwise
        close — see the harness's own docs on that limitation), so a failure to resume/close is
        logged and swallowed rather than raised from this cleanup path.
        """
        key = activity.info().workflow_run_id
        try:
            backend = backend_from_dict(input.backend)
            local_project_root = Path(input.local_project_root)
            session = await get_or_resume_session(input.ref, backend, local_project_root)
            await session.close()
        except Exception:
            activity.logger.warning(
                "sandbox_terminate: best-effort close failed for run %s (sandbox may already be "
                "gone)",
                key,
                exc_info=True,
            )
        finally:
            _SESSIONS.pop(key, None)


def sandbox_activities(
    backend_providers: Mapping[str, BackendProvider] | None = None,
) -> list[Callable[..., Any]]:
    """The bound sandbox lifecycle activities to register on a worker, with any backend-provider
    callables an agent on that worker names as its ``SandboxConfig.backend``.

    Tool-agnostic — one call per worker covers every agent it hosts, however many different agent
    types/backends they use (sessions are already disambiguated by workflow run id, so no
    per-agent/per-backend activity naming is needed). Mirrors ``harness/code_mode/activities.py``'s
    ``CODE_MODE_ACTIVITIES``, just as a factory since there's now something to inject::

        Worker(..., activities=[
            *sandbox_activities({"minted-token": daytona_with_minted_token}),
            agent.tool_activity(my_sandboxed_tool),
        ])

    Pass nothing when no agent on the worker uses a provider — that's what the ready-made
    :data:`SANDBOX_ACTIVITIES` is.
    """
    instance = SandboxActivities(backend_providers)
    return [instance.sandbox_activate, instance.sandbox_pause, instance.sandbox_terminate]


# The no-provider registration, for the common case::
#
#     Worker(..., activities=[*SANDBOX_ACTIVITIES, agent.tool_activity(my_sandboxed_tool), ...])
#
# Use `sandbox_activities({...})` instead (never both — the three activity names can be claimed
# only once per worker) when an agent on this worker names a provider as its
# `SandboxConfig.backend`.
SANDBOX_ACTIVITIES = sandbox_activities()
