"""Run the packaged harness web app as a process.

``app.py`` builds the FastAPI app; this module is the thin process layer around it — read the
``agents.toml`` files to register at startup and choose where the registry is kept, check Temporal
is actually reachable, then hand the app to uvicorn. Keeping that separate is what lets the app be
embedded (mounted under a path prefix, wrapped by another FastAPI instance) without inheriting a
uvicorn dependency or a CLI's opinions about hosts and ports.

The ``serve`` subcommand of the ``temporal-agent-kit`` console script is this module.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from temporal_agent_kit.ui import packaged_ui_dist
from temporal_agent_kit.web.client import verify_temporal_reachable
from temporal_agent_kit.web.registry import load_registrations

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(*registry_paths: Path, registry_store_endpoint: str | None = None) -> "FastAPI":
    """Build the packaged harness web app, registering the agents of ``registry_paths`` when it
    starts.

    Every file's agents are registered together, all or none. The registry is kept in the
    registry workflow, or with ``registry_store_endpoint`` in the registry store behind that
    Nexus endpoint (see :mod:`temporal_agent_kit.web.nexus_registry_store`). With no paths the app
    serves what the registry already holds.
    """
    # Imported here rather than at module scope so that importing this module (e.g. to reach
    # ``create_app`` from a custom launcher) doesn't require FastAPI — it only arrives with the
    # ``ui`` extra.
    from temporal_agent_kit.web import create_agent_kit_app
    from temporal_agent_kit.web.nexus_registry_store import nexus_registry_store
    from temporal_agent_kit.web.registry_workflow import workflow_registry_store

    registrations = [
        registration for path in registry_paths for registration in load_registrations(path)
    ]
    return create_agent_kit_app(
        registrations=registrations,
        registry_store=(
            workflow_registry_store
            if registry_store_endpoint is None
            else nexus_registry_store(registry_store_endpoint)
        ),
    )


def warn_without_console() -> None:
    """Say why there is no console when this install has none.

    The console ships in the published wheel and in Bazel's build, not in the source tree, so an
    install from a git checkout has none. The API is served anyway (pages like the examples' talk
    to it).
    """
    if packaged_ui_dist() is None:
        print(
            "No browser console in this install (temporal_agent_kit/ui/dist), so this server "
            "answers the API only. Install temporal-agent-kit from PyPI, which ships it, or "
            "from a checkout run `bazel run //python/temporal_agent_kit/web:cli -- serve ...`.",
            file=sys.stderr,
        )


def run_server(
    *registry_paths: Path,
    registry_store_endpoint: str | None = None,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> None:
    """Serve the packaged web app until interrupted, registering the agents of
    ``registry_paths`` when it starts. See :func:`create_app`."""
    # FastAPI/uvicorn ship with the `ui` extra, not the base install. Fail with the fix rather
    # than a bare ModuleNotFoundError, since calling this means the caller wants to serve.
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install shape
        raise SystemExit(
            "The Agent Kit web server needs the 'ui' extra: "
            "uv add 'temporal-agent-kit[ui]'"
        ) from exc

    app = create_app(*registry_paths, registry_store_endpoint=registry_store_endpoint)
    warn_without_console()

    # Pre-flight the Temporal connection. The app connects inside its ASGI lifespan, where a
    # failure surfaces as a starlette traceback plus "Application startup failed" — and only
    # after the port has been bound. Checking first reports an unreachable server with
    # ``connect_client``'s actionable message, before anything is listening.
    asyncio.run(verify_temporal_reachable())

    uvicorn.run(app, host=host, port=port)
