"""Shared FastAPI entrypoint for the bundled examples.

Every generic example serves the same packaged harness web app. Rather than copy-paste a one-line
``app.py`` into each example, this single module builds the app from one or more registry paths
passed as arguments:

    python -m examples.app <path/to/agents.toml> [more/agents.toml ...] [--host HOST] [--port PORT]

Each example's justfile ``server`` recipe points it at that example's ``agents.toml`` (a single
path) — so an example still runs standalone, serving only its own agent(s). The root justfile's
``server`` recipe passes *all* the examples' registries at once, merging them so the UI lists every
agent behind one server.

``create_app(*registry_paths)`` is also importable directly (e.g. for tests or a custom launcher).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI

from temporal_agent_harness.web import create_agent_harness_app
from temporal_agent_harness.web.registry import load_agent_registries


def create_app(*registry_paths: str | Path) -> FastAPI:
    """Build the packaged harness web app serving the merged agents from ``registry_paths``.

    A single path serves just that example's agent(s) (standalone behavior); multiple paths merge
    into one registry so every listed agent is selectable in the UI.
    """
    if not registry_paths:
        raise ValueError("create_app requires at least one registry path.")
    registry = load_agent_registries(registry_paths)
    return create_agent_harness_app(registry=registry)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "registry_paths",
        nargs="+",
        metavar="registry_path",
        help="Path(s) to agents.toml registries. One serves that example standalone; "
        "several are merged so the UI lists all their agents.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(*args.registry_paths), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
