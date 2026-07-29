# ABOUTME: SandboxConfig — the single place an agent definition picks which sandbox backend
# (Subprocess/E2B/Daytona) its `sandboxed=True` tools run in. Requires the optional `sandbox`
# extra (remote-box); importing this module (and thus `temporal_agent_harness.harness.sandbox`)
# is how an agent author opts into that dependency — core harness code (`agent_workflow.py`)
# never imports it.

from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel
from temporalio.workflow import ActivityConfig

_INSTALL_MESSAGE = (
    "Sandboxed tool support requires the optional `sandbox` extra (remote-box, which in turn "
    "requires Python >= 3.12). Install it with `uv sync --extra sandbox` or "
    "`pip install 'temporal-agent-harness[sandbox]'`."
)

try:
    from remote import AnyBackendConfig
except ImportError as exc:
    raise RuntimeError(_INSTALL_MESSAGE) from exc

BackendProvider = Callable[[], Awaitable[AnyBackendConfig]]
"""An async producer of a whole backend config, for a :class:`SandboxConfig` that can't state one
up front.

Takes nothing and returns the config to create the sandbox with — the way to supply values that
simply aren't knowable at import time (a minted token, a short-lived credential, a per-run
endpoint) because obtaining them needs I/O.

Providers are worker-side objects: they're registered by name when constructing the sandbox
lifecycle activities (``sandbox_activities({"name": provider})`` — see
``harness/sandbox/activities.py``), and an agent selects one by passing that name as its
:attr:`SandboxConfig.backend` instead of a config. See that field for when a provider runs.
"""


class SandboxConfig(BaseModel):
    """Which sandbox backend an agent's ``sandboxed=True`` tools run in, and how strictly.

    Passed once to ``AgentWorkflowRunner(..., sandbox=SandboxConfig(...))`` — the single place
    backend choice is made for a given agent. A ``sandboxed=True`` tool never chooses its own
    backend, so the same tool is reusable, unsandboxed or under any backend, across agents with
    zero code changes.

    Construct (and import ``remote.Subprocess``/``E2B``/``Daytona``) from inside an agent's
    ``@workflow.init`` under ``with workflow.unsafe.imports_passed_through():`` — remote-box (and
    the Daytona/E2B SDKs it depends on) is a third-party package with real I/O-capable imports,
    so it needs the same pass-through treatment any other harness module importing across a
    workflow boundary already uses (see ``harness/code_mode/tool.py`` for the established
    pattern)::

        with workflow.unsafe.imports_passed_through():
            from remote import Subprocess
            from temporal_agent_harness.harness import agent, AgentWorkflowRunner
            from temporal_agent_harness.harness.agent_protocol import AgentConfig
            from temporal_agent_harness.harness.sandbox import SandboxConfig

        SANDBOX = SandboxConfig(backend=Subprocess(), local_project_root=Path(__file__).parent)

    **Every** ``temporal_agent_harness`` import the workflow module needs — ``agent``,
    ``AgentWorkflowRunner``, ``agent_protocol`` (``AgentConfig``/``TextMessage``/etc.), not just
    the ones that obviously touch remote-box — must live in that SAME block. Importing even one
    of them separately, above/outside it, is enough on its own to make Temporal's workflow
    sandbox load two distinct copies of ``agent_workflow.py`` (its own restricted one, plus the
    pass-through one) — each with its own ``_CURRENT_RUNNER`` contextvar, so ``run_tool`` (set on
    one copy) becomes invisible to a sandboxed tool's approval-policy check (read on the other),
    surfacing as ``"tool ... has no active runner — it must be invoked via run_tool within an
    active turn"`` on every sandboxed tool call. Confirmed by direct repro — moving a single
    ``agent_protocol`` import outside the block was sufficient to reproduce it; moving it back in
    fixed it. Only reachable in a real workflow (Temporal's default ``SandboxedWorkflowRunner``);
    ``UnsandboxedWorkflowRunner()``, used throughout this harness's own test suite, never
    surfaces it.
    """

    backend: AnyBackendConfig | str
    """The backend config itself, OR the name of a worker-registered :data:`BackendProvider` that
    produces one.

    Pass a config whenever you can state it up front — that's the normal case, and the rest of this
    class assumes it. Pass a NAME when some field of it can only be obtained by doing I/O (a token
    minted from an external service, a short-lived credential, a per-run endpoint), so no literal
    could express it. A provider is a live async callable, which can neither be constructed in
    workflow code nor serialized into an activity's input, so the two halves are wired by name: the
    worker registers the callable, the agent names it::

        # ---- worker side: the provider, and the name it answers to
        async def daytona_with_minted_token() -> Daytona:
            token = await mint_token()  # a real network call; runs inside the activity
            return Daytona(
                snapshot_name="my-agent",
                dockerfile_path="Dockerfile.my-agent",
                env_vars={"TOKEN": token},   # injected into the sandbox at creation
            )

        Worker(..., activities=[
            *sandbox_activities({"minted-token": daytona_with_minted_token}),
            agent.tool_activity(my_sandboxed_tool),
        ])

        # ---- agent side: name it instead of declaring a config
        SANDBOX = SandboxConfig(backend="minted-token", local_project_root=Path(__file__).parent)

    The provider owns the WHOLE config — it isn't handed a base to amend — so keep the fields the
    image is built from (``snapshot_name``/``template_prefix``, ``dockerfile_path``,
    ``sandbox_class``) in agreement with what was built ahead of time, or activation will fail the
    ``require_prebuilt`` check. A name no worker registered also fails activation non-retryably,
    with the registered names listed, rather than silently sandboxing somewhere unintended.

    **When a provider runs:** exactly once per workflow run, inside the ``sandbox_activate``
    activity, on the first activation (the turn that actually creates this run's sandbox). Its
    result is returned to the workflow, persisted as workflow state, and threaded into every later
    sandbox-touching activity (activate/pause/terminate AND every sandboxed tool call, which
    re-supply the backend config so a worker that never activated this run can still reattach), so
    all of them agree on the one config for the run. Being recorded in activity history is also
    what makes it replay-safe: a provider does I/O, so it must never be re-run on replay, and it
    isn't.

    It CAN run more than once for a single sandbox, though: ``sandbox_activate`` is a Temporal
    activity like any other, so a retried attempt (worker crash, timeout) re-runs the provider
    before its result was ever recorded. Write it so a second call is harmless — mint a fresh token
    rather than consuming a one-shot resource.

    **Offline builds:** ``build_sandbox``/``check_sandbox`` can't run a provider (they're offline,
    and there's no worker), so for a provider-named backend they take the concrete config to build
    explicitly — ``build_sandbox(config, backend=Daytona(...))``.
    """

    local_project_root: Path

    require_prebuilt: bool = True
    """Runtime activation NEVER builds a missing image when this is True (the default, and the
    only recommended posture for production): it fails fast with a clear ``SandboxImageNotBuilt``
    error instead. Build (or verify) the image ahead of time, from CI or any offline script, with
    ``temporal_agent_harness.harness.sandbox.build_sandbox(config)`` /
    ``check_sandbox(config)`` — never by relaxing this flag in production. Set False only for
    local-dev convenience, where remote-box's own auto-build behavior (governed by the
    ``REMOTE_BOX_AUTO_BUILD`` env var / the backend config's own ``auto_build_override``) is
    allowed to build a missing image inline on first use.
    """

    activity_config: ActivityConfig | None = None
    """Applies to all three sandbox lifecycle activities (activate/pause/terminate). Can stay
    short — activation never builds when ``require_prebuilt`` is True, so there's no multi-minute
    image-build wait to allow for. Give it a bounded ``retry_policy`` if you want termination to
    give up (rather than retry indefinitely) against a genuinely unreachable backend, so an
    unreachable provider can't hang workflow shutdown forever.
    """
