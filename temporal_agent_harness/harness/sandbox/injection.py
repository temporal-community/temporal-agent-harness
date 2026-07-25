# ABOUTME: Delivery — how a sandbox reaches a tool body. ``attach_sandbox`` declares, per run,
# which backend and options this agent's sandbox uses; ``SandboxRef`` is the static marker an
# author puts in a tool's injections so the handle is resolved (and the sandbox claimed, lazily) at
# the moment a tool call dispatches.
#
# This is what makes the seam SDK-neutral: resolution happens in ``run_tool``, which every SDK
# integration already goes through, so no per-SDK sandbox wiring exists anywhere.
#
# Usage — declare the tool once, at module level::
#
#     @agent.tool_defn()
#     async def grep_workspace(sandbox: Injected[SandboxHandle], pattern: str) -> str:
#         """Search the workspace for `pattern`."""
#         result = await sandbox.exec("grep", "-rn", pattern, ".")
#         return result.stdout or f"no matches for {pattern!r}"
#
#     TOOL_INJECTIONS = {"sandbox": SandboxRef("workspace")}
#
# ...then, per run, say what "workspace" means for this agent::
#
#     attach_sandbox(runner, "workspace", MyOptions(tenant_id=config.tenant_id), hydrate=None)
#
# The sandbox is claimed on first use, so a run whose model never calls a sandbox tool never pays
# for one.

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio import workflow
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.sandbox import _activity_models as m
from temporal_agent_harness.harness.sandbox.handle import SandboxHandle
from temporal_agent_harness.harness.sandbox.protocol import SandboxOptions, SandboxState

if TYPE_CHECKING:
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

# Namespace for this module's entries in ``runner.injection_slots``, so sandbox state cannot
# collide with another LazyInjection implementation's.
_SLOT_PREFIX = "sandbox:"

# Claiming a sandbox can involve a cold start or an image pull, so it gets its own budget rather
# than inheriting the per-operation default.
_CREATE_CONFIG: ActivityConfig = ActivityConfig(start_to_close_timeout=timedelta(minutes=5))


class _SandboxSlot:
    """One run's sandbox: what to claim, and the state once claimed.

    Held in ``runner.injection_slots``. Claiming is deferred to first use and then memoised, so the
    sandbox is created at most once per run no matter how many tool calls touch it. ``state`` is
    plain workflow state, which is what makes this replay-safe — a replay reuses the recorded
    create result rather than provisioning a second sandbox.
    """

    def __init__(
        self,
        provider: str,
        options: SandboxOptions,
        *,
        hydrate: str | None = None,
        hydrate_on_claim: bool = False,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        self.provider = provider
        self.options = options
        self.hydrate = hydrate
        self.hydrate_on_claim = hydrate_on_claim
        self.activity_config = activity_config
        self.state: SandboxState | None = None

    async def handle(self) -> SandboxHandle:
        """The handle for this run's sandbox, claiming it if this is the first use."""
        if self.state is None:
            self.state = await workflow.execute_activity(
                m.activity_name(self.provider, m.CREATE),
                m.CreateArgs(options=self.options.model_dump(mode="json")),
                result_type=SandboxState,
                **(self.activity_config or _CREATE_CONFIG),
            )
            handle = SandboxHandle(self.provider, self.state, self.activity_config)
            if self.hydrate_on_claim:
                await handle.hydrate(self.hydrate)
            return handle
        return SandboxHandle(self.provider, self.state, self.activity_config)


def attach_sandbox(
    runner: AgentWorkflowRunner,
    provider: str,
    options: SandboxOptions,
    *,
    hydrate: str | None = None,
    hydrate_on_claim: bool = True,
    activity_config: ActivityConfig | None = None,
) -> None:
    """Declare, for this run, the sandbox that ``SandboxRef(provider)`` resolves to.

    Call it from workflow code — typically once, near the top of your ``@workflow.run`` — before
    the agent loop starts. Nothing is provisioned here: the sandbox is claimed on first use by a
    tool that actually needs it.

    Args:
        runner: This run's :class:`AgentWorkflowRunner`.
        provider: Name of the :class:`~...provider.SandboxProvider` registered on the worker. Also
            the key a :class:`SandboxRef` uses to find this declaration.
        options: Backend creation options — where per-run identity goes.
        hydrate: Locator passed to the backend's hydrate. ``None`` asks the backend to derive it
            from the sandbox's own identity, which is the common case.
        hydrate_on_claim: Whether to hydrate immediately after claiming. Leave True when the
            sandbox needs its input data before any tool reads it; set False for a backend with no
            hydration support, or when you want to control hydration explicitly.
        activity_config: Timeout/retry configuration for this sandbox's activities.

    Calling it twice for the same ``provider`` replaces the declaration, which also discards any
    sandbox already claimed under it — so do not use that to re-point a sandbox mid-run.
    """
    runner.injection_slots[_SLOT_PREFIX + provider] = _SandboxSlot(
        provider,
        options,
        hydrate=hydrate,
        hydrate_on_claim=hydrate_on_claim,
        activity_config=activity_config,
    )


class SandboxRef:
    """Static stand-in for a run's sandbox handle, resolved at tool dispatch.

    Put one in a tool's injections mapping at module level, next to the tool definition. It carries
    no per-run state itself — the per-run part lives on the runner via :func:`attach_sandbox` —
    which is what lets a single module-level toolset serve every concurrent run correctly.

    Implements the harness's ``LazyInjection`` protocol structurally, so ``run_tool`` resolves it
    without the harness core knowing anything about sandboxes.
    """

    def __init__(self, provider: str) -> None:
        """Reference the sandbox attached under ``provider`` for the current run."""
        self.provider = provider

    def __repr__(self) -> str:
        return f"SandboxRef({self.provider!r})"

    async def resolve_injection(self, runner: AgentWorkflowRunner) -> Any:
        """Return this run's :class:`SandboxHandle`, claiming the sandbox on first use."""
        slot = runner.injection_slots.get(_SLOT_PREFIX + self.provider)
        if slot is None:
            raise RuntimeError(
                f"no sandbox attached for provider {self.provider!r}: call "
                f"attach_sandbox(runner, {self.provider!r}, options) in your workflow before "
                f"running a turn whose tools inject SandboxRef({self.provider!r})"
            )
        return await slot.handle()


__all__ = ["SandboxRef", "attach_sandbox"]
