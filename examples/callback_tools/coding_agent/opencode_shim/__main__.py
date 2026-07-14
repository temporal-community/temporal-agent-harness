"""Run the shim: `python -m examples.callback_tools.coding_agent.opencode_shim [working_dir] [opts]`.

From this example's directory, `just serve` is the easy path.

The shim fronts the durable `CodingAgent` workflow via the packaged harness server (`--server`,
default http://localhost:8000). That server + the session-manager + the CodingAgent worker must be
running (see the justfile). `working_dir` is the project the agent works on (defaults to the
current directory).
"""

from __future__ import annotations

import argparse

import uvicorn

from .harness_backend import HarnessBackend
from .server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenCode-protocol shim server")
    parser.add_argument("working_dir", nargs="?", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4096)
    parser.add_argument(
        "--server",
        default="http://localhost:8000",
        help="Packaged harness HTTP server URL.",
    )
    args = parser.parse_args()

    backend = HarnessBackend(server_url=args.server, working_dir=args.working_dir or ".")
    app = create_app(backend=backend, working_dir=args.working_dir)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
