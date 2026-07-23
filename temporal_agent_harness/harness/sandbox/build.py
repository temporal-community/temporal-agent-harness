# ABOUTME: Offline/CI-only sandbox image build entry point. NEVER call this from a running
# worker or workflow — `sandbox/activities.py`'s `sandbox_activate` explicitly refuses to build
# at runtime by design (see `SandboxConfig.require_prebuilt`). Call `build_sandbox`/
# `check_sandbox` from a CI step or any offline script, once per agent's `SandboxConfig`, before
# deploying a worker that serves an agent configured with it.

_INSTALL_MESSAGE = (
    "Sandboxed tool support requires the optional `sandbox` extra (remote-box, which in turn "
    "requires Python >= 3.12). Install it with `uv sync --extra sandbox` or "
    "`pip install 'temporal-agent-harness[sandbox]'`."
)

try:
    import remote
    from remote.runtime import TargetResult, check_all, register_target
except ImportError as exc:
    raise RuntimeError(_INSTALL_MESSAGE) from exc

from temporal_agent_harness.harness.sandbox.config import SandboxConfig


def _pick(results: list[TargetResult], config: SandboxConfig) -> TargetResult:
    backend_name = config.backend.type.name.lower()
    for r in results:
        if r.backend == backend_name and r.project_root == config.local_project_root:
            return r
    raise RuntimeError(
        f"internal error: {config.backend.type.name}/{config.local_project_root} was just "
        "registered but is missing from the build/check results"
    )


def build_sandbox(config: SandboxConfig) -> TargetResult:
    """Build (or verify) the sandbox image ``config.backend``/``config.local_project_root``'s
    tools will run in.

    Run this from CI (or any offline script) BEFORE deploying a worker for an agent using this
    ``SandboxConfig`` — runtime activation refuses to build when ``require_prebuilt=True`` (the
    default) and fails fast with ``SandboxImageNotBuilt`` if this hasn't been run first::

        # a CI step, run once per agent definition, before any worker serving it is deployed
        from myagent.config import SANDBOX_CONFIG
        from temporal_agent_harness.harness.sandbox import build_sandbox

        result = build_sandbox(SANDBOX_CONFIG)
        assert result.status in ("built", "ready"), result.detail
    """
    register_target(config.backend, config.local_project_root)
    return _pick(remote.build_all(), config)


def check_sandbox(config: SandboxConfig) -> TargetResult:
    """Dry-run of :func:`build_sandbox`: reports whether the image is ready, without building
    anything. Useful as a CI gate step — fail the pipeline if a deploy would hit an unbuilt image.
    """
    register_target(config.backend, config.local_project_root)
    return _pick(check_all(), config)
