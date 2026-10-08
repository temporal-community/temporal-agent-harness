"""Workflow-side configuration for an agent's sandbox."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from agents.sandbox import Manifest
from agents.sandbox.capabilities import Capability
from agents.sandbox.session.sandbox_client import BaseSandboxClientOptions
from agents.sandbox.snapshot import LocalSnapshotSpec, SnapshotSpec
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness.sandbox._session import DEFAULT_AFFINITY_TIMEOUT

IdleAction = Literal["keep", "persist", "persist_and_shutdown", "pause"]

# Bounded retries, unlike Temporal's default, so a sandbox the backend refuses to delete
# cannot hold the agent's close open forever.
DEFAULT_SANDBOX_ACTIVITY_CONFIG = ActivityConfig(
    start_to_close_timeout=timedelta(minutes=5),
    retry_policy=RetryPolicy(maximum_attempts=5),
)


@dataclass(frozen=True)
class IdlePolicy:
    """What to do with the sandbox once the agent has been idle for ``after``.

    * ``"keep"``: nothing; the sandbox keeps running.
    * ``"persist"``: write the workspace to the snapshot; the sandbox keeps running.
    * ``"persist_and_shutdown"``: write the workspace to the snapshot, then shut the
      sandbox down to release compute. The next use resumes it, restoring the workspace
      from the snapshot.
    * ``"pause"``: shut down with the backend's native pause, which needs client options
      with ``pause_on_exit=True`` (E2B, Daytona, Runloop, Blaxel).

    Each idle period applies the action at most once.
    """

    after: timedelta
    action: IdleAction = "persist_and_shutdown"


@dataclass(frozen=True)
class SandboxConfig:
    """One durable sandbox for an agent, passed as ``AgentWorkflowRunner(sandbox=...)``.

    ``client`` names a ``SandboxClientProvider`` registered on the worker; ``options``,
    ``manifest`` and ``snapshot`` are what that provider's client creates the sandbox
    with. ``capabilities`` are OpenAI capabilities, built-in or your own:
    ``runner.sandbox_tools()`` turns their tools into harness tools, and each one's
    ``process_manifest`` applies to the sandbox's manifest.

    The sandbox is created lazily, on its first use. It is closed when the agent's run
    loop ends: stopped (a final persist) if ``keep_snapshot``, shut down, deleted, and,
    unless ``keep_snapshot``, its snapshot deleted where the snapshot store allows it.

    Snapshots must be readable by every worker, so a ``LocalSnapshotSpec`` (a directory on
    one worker) is rejected unless ``dev_mode=True``.
    """

    client: str
    options: BaseSandboxClientOptions | None = None
    manifest: Manifest | None = None
    snapshot: SnapshotSpec | None = None
    capabilities: Sequence[Capability] = ()
    idle: IdlePolicy | None = None
    keep_snapshot: bool = False
    dev_mode: bool = False
    activity_config: ActivityConfig = field(
        default_factory=lambda: ActivityConfig(**DEFAULT_SANDBOX_ACTIVITY_CONFIG)
    )
    affinity_timeout: timedelta = DEFAULT_AFFINITY_TIMEOUT

    def __post_init__(self) -> None:
        if isinstance(self.snapshot, LocalSnapshotSpec) and not self.dev_mode:
            raise ValueError(
                "SandboxConfig.snapshot is a LocalSnapshotSpec, a directory on one worker that "
                "other workers cannot read. Use a RemoteSnapshotSpec backed by shared storage, "
                "or pass dev_mode=True for a single-worker setup."
            )
        if (
            self.idle is not None
            and self.idle.action == "pause"
            and not getattr(self.options, "pause_on_exit", False)
        ):
            raise ValueError(
                "IdlePolicy(action='pause') needs client options with pause_on_exit=True; "
                "this backend or these options do not pause natively. Use "
                "'persist_and_shutdown' instead."
            )
        present = {c.type for c in self.capabilities}
        for capability in self.capabilities:
            missing = capability.required_capability_types() - present
            if missing:
                raise ValueError(
                    f"{type(capability).__name__} requires missing capabilities: "
                    f"{', '.join(sorted(missing))}"
                )
