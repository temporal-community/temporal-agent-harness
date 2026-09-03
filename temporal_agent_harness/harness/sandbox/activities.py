# ABOUTME: Worker-side activities backing sandboxed activity tools: activate (create-or-resume)
# at turn start, pause between turns, unconditional terminate on workflow shutdown. Also the
# shared session registry (`get_or_resume_session`) that `agent_workflow.py`'s sandboxed
# `activity_body` branch reaches into to run a tool call inside the same live sandbox.

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness.sandbox.config import BackendProvider, MicrosandboxBackend
from temporal_agent_harness.harness.sandbox import microsandbox_session
from temporal_agent_harness.harness.sandbox.microsandbox_session import (
    AnyBackend,
    SandboxSession,
    backend_from_dict,
    ensure_snapshot_ready,
    get_or_resume_session,
    run_tool_in_sandbox,
)
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

_SESSIONS = microsandbox_session._SESSIONS


def _from_session(session: SandboxSession) -> SandboxRef:
    return session.ref


class SandboxActivities:
    """The three sandbox lifecycle activities, holding the worker's backend-provider registry."""

    def __init__(self, backend_providers: Mapping[str, BackendProvider] | None = None) -> None:
        self._backend_providers: dict[str, BackendProvider] = dict(backend_providers or {})

    async def _resolve_backend(self, provider_name: str) -> AnyBackend:
        provider = self._backend_providers.get(provider_name)
        if provider is None:
            registered = ", ".join(sorted(self._backend_providers)) or "(none)"
            raise ApplicationError(
                f"no sandbox backend provider named {provider_name!r} is registered on this "
                f"worker; registered names: {registered}.",
                type="SandboxBackendProviderNotRegistered",
                non_retryable=True,
            )
        resolved = await provider()
        if resolved == "local":
            return "local"
        if not isinstance(resolved, MicrosandboxBackend):
            raise ApplicationError(
                f"sandbox backend provider {provider_name!r} returned {resolved!r}; it must return "
                "MicrosandboxBackend or 'local'",
                type="SandboxBackendProviderInvalidResult",
                non_retryable=True,
            )
        return resolved

    async def _get_session(
        self, ref: SandboxRef | None, backend: AnyBackend, local_project_root: Path
    ) -> SandboxSession:
        run_id = activity.info().workflow_run_id
        session = await get_or_resume_session(
            ref, backend, local_project_root, workflow_run_id=run_id
        )
        _SESSIONS[run_id] = session
        return session

    @activity.defn(name=SANDBOX_ACTIVATE_ACTIVITY)
    async def sandbox_activate(self, input: SandboxActivateInput) -> SandboxRefResult:
        resolved_backend: dict[str, Any] | None = None
        if isinstance(input.backend, str):
            backend: AnyBackend = await self._resolve_backend(input.backend)
            resolved_backend = (
                {"type": "local"}
                if backend == "local"
                else backend.model_dump(mode="json")
            )
        else:
            backend = backend_from_dict(input.backend)

        local_project_root = Path(input.local_project_root)
        if input.require_prebuilt and isinstance(backend, MicrosandboxBackend):
            try:
                await ensure_snapshot_ready(backend)
            except FileNotFoundError as exc:
                raise ApplicationError(
                    f"Sandbox image not built: {exc}. Build it offline first with "
                    "temporal_agent_harness.harness.sandbox.build_sandbox(config).",
                    type="SandboxImageNotBuilt",
                    non_retryable=True,
                ) from exc

        session = await self._get_session(input.ref, backend, local_project_root)
        await session.start()
        async with session:
            pass
        return SandboxRefResult(ref=_from_session(session), backend=resolved_backend)

    @activity.defn(name=SANDBOX_PAUSE_ACTIVITY)
    async def sandbox_pause(self, input: SandboxPauseInput) -> SandboxRefResult:
        backend = backend_from_dict(input.backend)
        local_project_root = Path(input.local_project_root)
        session = await self._get_session(input.ref, backend, local_project_root)
        await session.pause()
        return SandboxRefResult(ref=_from_session(session))

    @activity.defn(name=SANDBOX_TERMINATE_ACTIVITY)
    async def sandbox_terminate(self, input: SandboxTerminateInput) -> None:
        key = activity.info().workflow_run_id
        try:
            backend = backend_from_dict(input.backend)
            local_project_root = Path(input.local_project_root)
            session = await self._get_session(input.ref, backend, local_project_root)
            await session.close()
        except Exception:
            activity.logger.warning(
                "sandbox_terminate: best-effort close failed for run %s",
                key,
                exc_info=True,
            )
        finally:
            _SESSIONS.pop(key, None)


def sandbox_activities(
    backend_providers: Mapping[str, BackendProvider] | None = None,
) -> list[Callable[..., Any]]:
    instance = SandboxActivities(backend_providers)
    return [instance.sandbox_activate, instance.sandbox_pause, instance.sandbox_terminate]


SANDBOX_ACTIVITIES = sandbox_activities()
