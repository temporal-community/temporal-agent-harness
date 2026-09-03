# ABOUTME: Offline/CI-only sandbox snapshot build entry point. NEVER call from a running worker
# or workflow — sandbox_activate refuses to build at runtime when require_prebuilt=True.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from temporal_agent_harness.harness.sandbox.config import MicrosandboxBackend, SandboxConfig

try:
    from microsandbox import Snapshot
    from microsandbox.errors import MicrosandboxError
except ImportError:
    Snapshot = None  # type: ignore[misc, assignment]
    MicrosandboxError = Exception  # type: ignore[misc, assignment]

BuildTarget = Literal["local"] | MicrosandboxBackend


@dataclass
class TargetResult:
    backend: str
    project_root: Path
    status: str
    detail: str


def _target_backend(
    config: SandboxConfig, backend: MicrosandboxBackend | None
) -> BuildTarget:
    if config.backend == "local":
        return "local"
    if not isinstance(config.backend, str):
        if backend is not None:
            raise ValueError(
                "build_sandbox/check_sandbox got an explicit backend= for a SandboxConfig that "
                "already declares a concrete backend."
            )
        return config.backend
    if backend is None:
        raise ValueError(
            f"SandboxConfig.backend is the provider name {config.backend!r}; pass the config "
            "to build explicitly — build_sandbox(config, backend=MicrosandboxBackend(...))."
        )
    return backend


def _local_result(config: SandboxConfig) -> TargetResult:
    return TargetResult(
        backend="local",
        project_root=config.local_project_root,
        status="ready",
        detail="local backend runs in-process — no snapshot to build",
    )


async def _snapshot_exists(name: str) -> bool:
    if Snapshot is None:
        return False
    try:
        await Snapshot.open(name)
        return True
    except MicrosandboxError:
        return False


async def _check_microsandbox(config: SandboxConfig, backend: MicrosandboxBackend) -> TargetResult:
    if not backend.snapshot_name:
        return TargetResult(
            backend="microsandbox",
            project_root=config.local_project_root,
            status="ready",
            detail="no snapshot_name configured",
        )
    if await _snapshot_exists(backend.snapshot_name):
        return TargetResult(
            backend="microsandbox",
            project_root=config.local_project_root,
            status="ready",
            detail=f"snapshot {backend.snapshot_name!r} exists",
        )
    return TargetResult(
        backend="microsandbox",
        project_root=config.local_project_root,
        status="missing",
        detail=f"snapshot {backend.snapshot_name!r} not found",
    )


def build_sandbox(
    config: SandboxConfig, backend: MicrosandboxBackend | None = None
) -> TargetResult:
    """Build or verify the sandbox snapshot for ``config`` — offline/CI only."""
    target = _target_backend(config, backend)
    if target == "local":
        return _local_result(config)
    # ponytail: full dockerfile→sandbox→snapshot pipeline lands in Phase 3 example wiring.
    return asyncio.run(_check_microsandbox(config, target))


def check_sandbox(
    config: SandboxConfig, backend: MicrosandboxBackend | None = None
) -> TargetResult:
    """Dry-run: report whether the snapshot is ready without building."""
    target = _target_backend(config, backend)
    if target == "local":
        return _local_result(config)
    return asyncio.run(_check_microsandbox(config, target))
