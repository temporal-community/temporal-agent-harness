"""Sandbox selection for the coding agent.

The agent's shell and file edits run inside a sandbox, so model-driven commands
cannot touch the host beyond the sandbox. Two backends, chosen by
``CODING_AGENT_SANDBOX``:

- ``docker`` (default): a throwaway container per session. Real isolation; needs
  a Docker daemon. The image is ``CODING_AGENT_SANDBOX_IMAGE`` (default
  ``python:3.12-slim``).
- ``local``: the OpenAI Agents SDK's unix-local backend. It runs the same tools
  directly on the host with NO isolation, for a machine you already trust (your
  own laptop, working on your own repo) or where Docker is not available.

These are the OpenAI Agents SDK's own sandbox backends. The durable worker wraps
the chosen one in the harness's ``temporal_sandbox_client`` so sandbox
operations become activities; the local runner uses it directly. Keep the
setting the same on every worker that serves one session: a session's sandbox
lives on the worker that created it (see the README's note on placement).
"""

from __future__ import annotations

import os
from pathlib import Path

from agents import RunConfig
from agents.sandbox import Manifest, SandboxRunConfig
from agents.sandbox.session.sandbox_client import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
)

# The provider name the worker registers and the workflow references. One
# constant so the two sides cannot drift.
SANDBOX_NAME = "coding-sandbox"


def sandbox_kind() -> str:
    return os.environ.get("CODING_AGENT_SANDBOX", "docker").strip().lower()


def workspace_root() -> Path:
    """The project the agent works on. Search reads it; the local backend edits it in place."""
    return Path(os.environ.get("CODING_AGENT_WORKSPACE", ".")).resolve()


def sandbox_manifest(kind: str | None = None) -> Manifest | None:
    """Root the LOCAL backend at the project so the agent edits it in place; None otherwise.

    The docker backend cannot bind-mount a host directory (its SDK options expose no mount),
    so it keeps an isolated workspace. The unix-local backend, given a caller-provided root, is
    never deleted or cleared by the SDK (``workspace_root_owned`` stays False and we use no
    snapshot), so edits land in the real repo and persist on disk. This is opt-in: it needs both
    ``CODING_AGENT_SANDBOX=local`` and ``CODING_AGENT_WORKSPACE`` set.
    """
    kind = kind or sandbox_kind()
    if kind == "local" and os.environ.get("CODING_AGENT_WORKSPACE"):
        return Manifest(root=str(workspace_root()))
    return None


def _image() -> str:
    return os.environ.get("CODING_AGENT_SANDBOX_IMAGE", "python:3.12-slim")


def build_sandbox_client(kind: str | None = None) -> BaseSandboxClient:
    """The real sandbox backend (registered on a worker, or used directly by the
    local runner). Imports are local so a worker that never uses Docker does not
    import the Docker SDK, and vice versa."""
    kind = kind or sandbox_kind()
    if kind == "docker":
        import docker
        from agents.sandbox.sandboxes.docker import DockerSandboxClient

        return DockerSandboxClient(docker_client=docker.from_env())
    if kind == "local":
        from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

        return UnixLocalSandboxClient()
    raise ValueError(f"CODING_AGENT_SANDBOX must be 'docker' or 'local', got {kind!r}")


def sandbox_options(kind: str | None = None) -> BaseSandboxClientOptions:
    """Options matching the active backend. Safe to build in workflow code (a
    plain config object; it constructs no client)."""
    kind = kind or sandbox_kind()
    if kind == "docker":
        from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

        return DockerSandboxClientOptions(image=_image())
    if kind == "local":
        from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClientOptions

        return UnixLocalSandboxClientOptions()
    raise ValueError(f"CODING_AGENT_SANDBOX must be 'docker' or 'local', got {kind!r}")


def local_run_config() -> RunConfig:
    """RunConfig for the no-Temporal local runner: the real client, used directly."""
    return RunConfig(
        sandbox=SandboxRunConfig(
            client=build_sandbox_client(),
            options=sandbox_options(),
            manifest=sandbox_manifest(),
        )
    )
