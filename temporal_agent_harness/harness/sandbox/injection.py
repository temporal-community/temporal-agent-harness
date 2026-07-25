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
from typing import TYPE_CHECKING, Any, Literal

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


# What to do when the sandbox behind a run turns out to have been reclaimed.
#
#   "fail"      — let the failure reach the tool (and so the model). Correct whenever the workspace
#                 held anything the model wrote, because replacing it with an empty sandbox would
#                 present lost work as success.
#   "reacquire" — claim a fresh sandbox, re-hydrate it, and retry the operation once. Correct when
#                 the workspace is entirely derived from the hydration source, since the replacement
#                 is then indistinguishable from the original.
#
# Only the author knows which case they are in, which is why this is a parameter and not a default
# the harness picks.
OnReclaim = Literal["fail", "reacquire"]

# What to do with the sandbox when the agent's turn loop ends.
#
#   "keep"   — leave it. Right for a backend whose sandboxes are addressed by a durable identity
#              (a tenant, a conversation, a repository) and deliberately outlive one run so the next
#              run reuses the warm workspace. Lifetime is then the backend's TTL, not the agent's.
#   "delete" — release it. Right when the sandbox is genuinely run-scoped, so its lifetime — and
#              the lifetime of whatever data was hydrated into it — tracks the agent's rather than
#              an external reaper's.
#
# "keep" is the default because deleting a sandbox a backend intends to reuse is destructive and
# silent, whereas keeping one that could have been released is merely wasteful.
OnComplete = Literal["keep", "delete"]


class _SandboxSlot:
    """One run's sandbox: what to claim, and the state once claimed.

    Held in ``runner.injection_slots``. Claiming is deferred to first use and then memoised, so the
    sandbox is created at most once per run no matter how many tool calls touch it. ``state`` is
    plain workflow state, which is what makes this replay-safe — a replay reuses the recorded
    create result rather than provisioning a second sandbox.

    Also the :class:`ReclaimRecovery` for the handles it hands out, since replacing a reclaimed
    sandbox means re-running the claim this slot owns.
    """

    def __init__(
        self,
        provider: str,
        options: SandboxOptions,
        *,
        hydrate: str | None = None,
        hydrate_on_claim: bool = False,
        activity_config: ActivityConfig | None = None,
        on_reclaim: OnReclaim = "fail",
    ) -> None:
        self.provider = provider
        self.options = options
        self.hydrate = hydrate
        self.hydrate_on_claim = hydrate_on_claim
        self.activity_config = activity_config
        self.on_reclaim: OnReclaim = on_reclaim
        self.state: SandboxState | None = None

    async def handle(self) -> SandboxHandle:
        """The handle for this run's sandbox, claiming it if this is the first use."""
        if self.state is None:
            return await self._claim()
        return self._handle_for(self.state)

    async def recover_from_reclaim(self) -> SandboxState | None:
        """Replace a reclaimed sandbox, or ``None`` under the ``"fail"`` policy.

        Discards the dead identity first, so a failure to re-claim leaves the slot empty rather
        than pointing at a sandbox that no longer exists — the next tool call then attempts a fresh
        claim instead of inheriting a known-bad one.
        """
        if self.on_reclaim != "reacquire":
            return None
        self.state = None
        await self._claim()
        return self.state

    async def _claim(self) -> SandboxHandle:
        """Claim (or re-claim) the sandbox, hydrating it if configured, and return its handle."""
        self.state = await workflow.execute_activity(
            m.activity_name(self.provider, m.CREATE),
            m.CreateArgs(options=self.options.model_dump(mode="json")),
            result_type=SandboxState,
            **(self.activity_config or _CREATE_CONFIG),
        )
        handle = self._handle_for(self.state)
        if self.hydrate_on_claim:
            await handle.hydrate(self.hydrate)
        return handle

    async def release(self) -> None:
        """Delete the claimed sandbox, if one was ever claimed. Idempotent."""
        if self.state is None:
            return
        state, self.state = self.state, None
        await self._handle_for(state).delete()

    def _handle_for(self, state: SandboxState) -> SandboxHandle:
        # Pass self as the recovery only when recovery is actually enabled, so the handle's
        # error path stays a plain re-raise under the default policy.
        return SandboxHandle(
            self.provider,
            state,
            self.activity_config,
            recovery=self if self.on_reclaim == "reacquire" else None,
        )


def attach_sandbox(
    runner: AgentWorkflowRunner,
    provider: str,
    options: SandboxOptions,
    *,
    hydrate: str | None = None,
    hydrate_on_claim: bool = True,
    activity_config: ActivityConfig | None = None,
    on_reclaim: OnReclaim = "fail",
    on_complete: OnComplete = "keep",
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
        on_reclaim: What to do if the sandbox is reclaimed mid-run — ``"fail"`` (default) surfaces
            the failure to the tool and so to the model; ``"reacquire"`` transparently claims and
            re-hydrates a replacement, then retries the operation once. Choose ``"reacquire"`` only
            when the workspace is entirely derived from the hydration source; if the model writes
            state you care about, ``"fail"`` is what stops lost work from looking like success.
        on_complete: Whether to ``"delete"`` the sandbox when the agent's turn loop ends, or
            ``"keep"`` it (default) and leave its lifetime to the backend. Use ``"delete"`` when the
            sandbox is run-scoped, so the data hydrated into it does not outlive the run that needed
            it. Note that a hard workflow terminate skips completion hooks, so a backend-side TTL is
            still the backstop rather than an optimisation.

    Raises:
        RuntimeError: if a sandbox has already been claimed for ``provider`` under DIFFERENT
            options. Re-attaching different identity mid-run is almost always a bug — the slot
            memoises its claim, so the tools would keep using the first sandbox while the code reads
            as though it had switched. Use :func:`discard_sandbox` to release the claim first, and
            note that one sandbox per provider per run means an agent handling multiple subjects
            needs a provider (or a run) per subject.

    Re-attaching with identical options after a claim is a no-op, so calling this from a handler that
    may run more than once is safe.
    """
    key = _SLOT_PREFIX + provider
    existing = runner.injection_slots.get(key)
    if existing is not None and existing.state is not None:
        if existing.options.model_dump(mode="json") == options.model_dump(mode="json"):
            return
        raise RuntimeError(
            f"a sandbox is already claimed for provider {provider!r} under different options "
            f"(claimed={existing.state.backend_ref!r}). The claim is memoised for the run, so the "
            f"new options would be ignored and tools would keep using the claimed sandbox. Call "
            f"discard_sandbox(runner, {provider!r}) first if replacing it is intended."
        )

    slot = _SandboxSlot(
        provider,
        options,
        hydrate=hydrate,
        hydrate_on_claim=hydrate_on_claim,
        activity_config=activity_config,
        on_reclaim=on_reclaim,
    )
    runner.injection_slots[key] = slot
    if on_complete == "delete":
        runner.add_completion_hook(slot.release)


def discard_sandbox(runner: AgentWorkflowRunner, provider: str) -> bool:
    """Forget the sandbox claimed for ``provider``, so the next tool call claims a fresh one.

    The escape hatch for the ``"fail"`` policy: a workflow that catches a reclaim failure and
    decides, with knowledge the harness does not have, that continuing on a new workspace is right
    can call this and carry on. The declaration from :func:`attach_sandbox` is kept — only the
    claimed identity is dropped — and the replacement is hydrated on claim just like the original.

    Returns True if a sandbox was attached (whether or not it had been claimed yet), False if
    nothing was attached under that name. Does not delete the old sandbox: it is presumed gone, and
    a backend with idempotent claim may hand back the very same one.
    """
    slot = runner.injection_slots.get(_SLOT_PREFIX + provider)
    if slot is None:
        return False
    slot.state = None
    return True


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


__all__ = ["OnComplete", "OnReclaim", "SandboxRef", "attach_sandbox", "discard_sandbox"]
