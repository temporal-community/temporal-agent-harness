"""Run the packaged harness web app as a process.

``app.py`` builds the FastAPI app; this module is the thin process layer around it — resolve
``agents.toml`` registries into an app, check Temporal is actually reachable, then hand the app
to uvicorn. Keeping that separate is what lets the app be embedded (mounted under a path prefix,
wrapped by another FastAPI instance) without inheriting a uvicorn dependency or a CLI's opinions
about hosts and ports.

The ``serve`` subcommand of the ``temporal-agent-harness`` console script is this module.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from temporal_agent_harness.web.client import verify_temporal_reachable
from temporal_agent_harness.web.registry import load_agent_registries

if TYPE_CHECKING:
    from fastapi import FastAPI


def create_app(*registry_paths: str | Path) -> "FastAPI":
    """Build the packaged harness web app serving the merged agents from ``registry_paths``.

    A single path serves just that registry's agent(s); multiple paths merge into one registry so
    every listed agent is selectable in the UI.
    """
    # Imported here rather than at module scope so that importing this module (e.g. to reach
    # ``create_app`` from a custom launcher) doesn't require FastAPI — it only arrives with the
    # ``ui`` extra.
    from temporal_agent_harness.web import create_agent_harness_app

    if not registry_paths:
        raise ValueError("create_app requires at least one registry path.")
    registry = load_agent_registries(registry_paths)
    return create_agent_harness_app(registry=registry)


def run_server(
    *registry_paths: str | Path,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> None:
    """Serve the packaged web app over ``registry_paths`` until interrupted."""
    # FastAPI/uvicorn ship with the `ui` extra, not the base install. Fail with the fix rather
    # than a bare ModuleNotFoundError, since calling this means the caller wants to serve.
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install shape
        raise SystemExit(
            "The harness web server needs the 'ui' extra: "
            "uv add 'temporal-agent-harness[ui]'"
        ) from exc

    app = create_app(*registry_paths)

    # Pre-flight the Temporal connection. The app connects inside its ASGI lifespan, where a
    # failure surfaces as a starlette traceback plus "Application startup failed" — and only
    # after the port has been bound. Checking first means an unreachable server reports the same
    # actionable message the session-manager entrypoint gives, before anything is listening.
    asyncio.run(verify_temporal_reachable())

    uvicorn.run(app, host=host, port=port)
