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


def _target_backend(
    config: SandboxConfig, backend: "remote.AnyBackendConfig | None"
) -> "remote.AnyBackendConfig":
    """The concrete backend config to build against.

    ``SandboxConfig.backend`` may be the NAME of a worker-registered provider instead of a config
    (for backends whose fields need I/O to determine). Nothing here can run such a provider — these
    functions are offline, with no worker and no registry — so that case requires the caller to say
    which config to build, and says so plainly rather than failing deeper in remote-box.
    """
    if not isinstance(config.backend, str):
        if backend is not None:
            raise ValueError(
                "build_sandbox/check_sandbox got an explicit backend= for a SandboxConfig that "
                "already declares a concrete backend; pass backend= only when the config names a "
                "provider instead."
            )
        return config.backend
    if backend is None:
        raise ValueError(
            f"this SandboxConfig's backend is the provider name {config.backend!r}, not a config, "
            "and a provider can only run inside a worker's sandbox_activate activity. Pass the "
            "config to build explicitly — e.g. build_sandbox(config, backend=Daytona(...)) — "
            "keeping every image-determining field (snapshot_name/template_prefix, "
            "dockerfile_path, sandbox_class) identical to what the provider will return."
        )
    return backend


def _pick(
    results: list[TargetResult], backend: "remote.AnyBackendConfig", config: SandboxConfig
) -> TargetResult:
    backend_name = backend.type.name.lower()
    for r in results:
        if r.backend == backend_name and r.project_root == config.local_project_root:
            return r
    raise RuntimeError(
        f"internal error: {backend.type.name}/{config.local_project_root} was just "
        "registered but is missing from the build/check results"
    )


def build_sandbox(
    config: SandboxConfig, backend: "remote.AnyBackendConfig | None" = None
) -> TargetResult:
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

    ``backend`` is required only when the config's own ``backend`` is a provider NAME — nothing
    offline can run a provider, so CI states the config to build instead::

        build_sandbox(SANDBOX_CONFIG, backend=Daytona(snapshot_name="my-agent", ...))
    """
    target = _target_backend(config, backend)
    register_target(target, config.local_project_root)
    return _pick(remote.build_all(), target, config)


def check_sandbox(
    config: SandboxConfig, backend: "remote.AnyBackendConfig | None" = None
) -> TargetResult:
    """Dry-run of :func:`build_sandbox`: reports whether the image is ready, without building
    anything. Useful as a CI gate step — fail the pipeline if a deploy would hit an unbuilt image.
    """
    target = _target_backend(config, backend)
    register_target(target, config.local_project_root)
    return _pick(check_all(), target, config)
