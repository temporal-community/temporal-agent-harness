"""Shared FastAPI entrypoint for the bundled examples.

Every example serves the same packaged harness web app — the only per-example difference is which
``agents.toml`` registry it exposes. So rather than copy-paste a one-line ``app.py`` into each
example, this single module builds the app from a registry path passed as an argument; each
example's justfile ``server`` recipe points it at that example's ``agents.toml``:

    python -m examples.app <path/to/agents.toml> [--host HOST] [--port PORT]

``create_app(registry_path)`` is also importable directly (e.g. for tests or a custom launcher).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import FastAPI

from temporal_agent_harness.web import create_agent_harness_app


def create_app(registry_path: str | Path) -> FastAPI:
    """Build the packaged harness web app for the example whose registry is at ``registry_path``."""
    return create_agent_harness_app(registry_path=registry_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry_path", help="Path to the example's agents.toml registry.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(args.registry_path), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
