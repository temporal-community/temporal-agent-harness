# ABOUTME: The SDK-neutral sandbox contract — the types a backend implements and the errors it
# raises. Nothing here imports an AI SDK, so one backend implementation serves every SDK the
# harness wraps. A backend is worker-side and does real I/O; the workflow side never touches it
# directly (see handle.py).
#
# Implement :class:`SandboxBackend` for your sandbox provider, declare its ``options_model``, and
# register it with a :class:`~temporal_agent_harness.harness.sandbox.provider.SandboxProvider`.
# Implement :class:`SupportsHydration` as well when your backend can read your data store itself —
# that keeps workspace contents out of activity payloads entirely.
#
# No ``from __future__ import annotations``: these models cross Temporal's pydantic data converter,
# which needs concrete annotations (same constraint as harness/code_mode/batch_models.py).

from typing import Any, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class SandboxState(BaseModel):
    """The serializable identity of one sandbox — the only sandbox data that enters history.

    ``backend_ref`` is opaque to the harness: put in it whatever your backend needs to find this
    sandbox again. A backend whose claim is idempotent on caller-supplied identity (one sandbox per
    tenant, per conversation, per repository) can store that identity here and get replay-safety
    structurally — any retry, on any worker, re-derives the same sandbox with no id to track.

    ``attributes`` carries backend-specific detail the backend wants back on later calls. It is
    persisted in workflow history, so keep it small and free of anything sensitive.
    """

    backend_ref: str
    supports_pty: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)


class SandboxOptions(BaseModel):
    """Base for a backend's creation options. Subclass it and point ``options_model`` at yours.

    Subclasses are serialized structurally (as a plain mapping) rather than polymorphically, and
    the provider validates them back into the backend's declared ``options_model`` worker-side —
    so no pydantic discriminator plumbing is needed to carry a subclass across the boundary.
    """

    model_config = ConfigDict(extra="allow")


class ExecResult(BaseModel):
    """The outcome of one command or code execution inside the sandbox."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        """True when the command exited zero."""
        return self.exit_code == 0


class FsEntry(BaseModel):
    """One entry from a sandbox directory listing."""

    path: str
    is_dir: bool = False
    size: int | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SandboxError(Exception):
    """A sandbox operation failed.

    ``retryable`` decides how the provider surfaces it: a retryable error propagates so Temporal's
    activity retry policy backs off and tries again, while a non-retryable one becomes a
    non-retryable ``ApplicationError`` so the workflow sees a terminal failure. Default is
    retryable, because most sandbox failures are transient (a cold start, a busy backend, a
    provider rate limit).
    """

    retryable: bool = True
    code: str = "sandbox_error"


class SandboxUnavailable(SandboxError):
    """The backend could not provide a sandbox right now — quota, rate limit, capacity.

    Retryable on purpose: with a retry policy this is already a queue. When sandboxes are a scarce
    resource, route sandbox activities to their own task queue and size worker concurrency to
    capacity, which turns backoff into bounded admission control.
    """

    retryable = True
    code = "sandbox_unavailable"


class SandboxReclaimed(SandboxError):
    """The sandbox this run had been using is gone, and anything written into it is lost.

    Distinct from "the call failed", and the distinction matters: sandboxes are reclaimed by idle
    and hard TTLs, backend garbage collection, node drain, and spot preemption. Durable execution
    gives you a durable *log*, not a durable *filesystem* — so a blind retry after a reclaim hands
    the agent a fresh empty workspace with no signal that its earlier writes vanished.

    Non-retryable for that reason. A caller whose workspace held only derived state can catch this,
    re-acquire, re-hydrate, and continue deliberately; a caller that had un-persisted work should
    tell the model what was lost rather than pretend otherwise.
    """

    retryable = False
    code = "sandbox_reclaimed"


# ---------------------------------------------------------------------------
# The backend contract
# ---------------------------------------------------------------------------


@runtime_checkable
class SandboxBackend(Protocol):
    """Worker-side sandbox implementation: real I/O against a real sandbox.

    Every method may raise :class:`SandboxError` (or a subclass); the provider translates those
    into the right Temporal failure. Methods are handed the :class:`SandboxState` they were given
    at ``create``, so an implementation can be stateless and rebuild whatever handle it needs —
    which is what lets a fresh worker pick up an in-flight run.
    """

    options_model: type[SandboxOptions]
    """The concrete options type this backend expects at ``create``."""

    async def create(self, options: SandboxOptions) -> SandboxState:
        """Provision (or claim) a sandbox and return its state.

        Backends with idempotent claim semantics should return the existing sandbox rather than a
        duplicate; that makes ``create`` safe to call on every retry.
        """
        ...

    async def resume(self, state: SandboxState) -> SandboxState:
        """Re-attach to an existing sandbox, returning its current state.

        Called when a worker sees a ``SandboxState`` it has no live handle for — after a restart,
        a redeploy, or when a different worker picks up the run. Raise
        :class:`SandboxReclaimed` if the sandbox no longer exists.
        """
        ...

    async def delete(self, state: SandboxState) -> None:
        """Tear the sandbox down. Should be idempotent."""
        ...

    async def exec(
        self,
        state: SandboxState,
        command: list[str],
        timeout: float | None = None,
    ) -> ExecResult:
        """Run a command in the sandbox."""
        ...

    async def run_code(
        self,
        state: SandboxState,
        code: str,
        language: str = "python",
        timeout: float | None = None,
    ) -> ExecResult:
        """Execute a code blob in the sandbox."""
        ...

    async def read(self, state: SandboxState, path: str) -> bytes:
        """Read one file's bytes."""
        ...

    async def write(self, state: SandboxState, files: Mapping[str, bytes]) -> int:
        """Write ``{path: content}`` into the sandbox; return bytes written."""
        ...

    async def ls(self, state: SandboxState, path: str, depth: int = 1) -> list[FsEntry]:
        """List entries under ``path`` up to ``depth`` levels."""
        ...

    async def running(self, state: SandboxState) -> bool:
        """Whether the sandbox is currently usable."""
        ...


@runtime_checkable
class SupportsHydration(Protocol):
    """Optional backend capability: move workspace data by *reference* instead of by value.

    Implement this when the backend can reach your data store directly (an object-store bucket, a
    volume, a snapshot service). The payload crossing the activity boundary is then a locator
    rather than the data, which matters for two independent reasons:

    * **Size.** Workspace contents never hit the payload limit and never need a large-payload
      codec, because they never become a payload.
    * **Copies.** By-value transfer creates an additional persisted copy of the data in whatever
      store the codec offloads to, with its own lifecycle, retention, and replay coupling. By
      reference there is one copy, in the store you already govern.

    ``locator=None`` means "whatever this sandbox's own identity implies" — the natural default for
    a backend whose sandboxes are keyed on caller-supplied identity. The harness never interprets a
    locator: make it whatever addresses your data (an object-store prefix, a git ref, a snapshot id).

    One implementation choice worth making deliberately, because it decides how this scales: whether
    the data flows *through* your control plane (list the objects, then write them into the sandbox)
    or is pulled by the sandbox *itself* (credentials or a mount inside it, or pre-signed URLs). The
    first is simpler and keeps credentials out of the sandbox; the second is what holds up when the
    workspace is large, since the proxying process otherwise materializes the whole thing. A backend
    hydrating a handful of small files can take the easy path; one checking out a repository probably
    cannot.
    """

    async def hydrate(self, state: SandboxState, locator: str | None = None) -> int:
        """Populate the sandbox from ``locator``; return the number of files written."""
        ...

    async def persist(self, state: SandboxState, locator: str | None = None) -> str:
        """Check the workspace out to ``locator``; return a reference that ``hydrate`` accepts.

        Worth knowing when you implement this: a checkpoint that only fires on graceful shutdown
        captures nothing when a pod dies unexpectedly. If the workspace holds state you cannot
        rebuild, call this on a cadence rather than only at the end.
        """
        ...


__all__ = [
    "ExecResult",
    "FsEntry",
    "SandboxBackend",
    "SandboxError",
    "SandboxOptions",
    "SandboxReclaimed",
    "SandboxState",
    "SandboxUnavailable",
    "SupportsHydration",
]
