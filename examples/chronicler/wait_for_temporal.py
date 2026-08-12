"""Block until the local Temporal frontend accepts TCP, then exit 0.

`just dev` starts every process at once via honcho, so the server / workers would otherwise race
the `temporal server start-dev` boot and fail their first connect. The Procfile runs this guard
before each Temporal-dependent process.

Polls localhost:7233 (override with CHRONICLER_TEMPORAL_HOST / _PORT) for up to 30s, then starts
anyway so a remote/bring-your-own Temporal isn't blocked — temporalio's own client-connect retry
handles the rest. Set CHRONICLER_SKIP_WAIT=1 to skip entirely.
"""

import os
import socket
import sys
import time

if os.environ.get("CHRONICLER_SKIP_WAIT"):
    sys.exit(0)

host = os.environ.get("CHRONICLER_TEMPORAL_HOST", "localhost")
port = int(os.environ.get("CHRONICLER_TEMPORAL_PORT", "7233"))
deadline = time.monotonic() + 30.0

while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=1):
            sys.exit(0)
    except OSError:
        time.sleep(0.5)

print(
    f"[wait_for_temporal] {host}:{port} not reachable after 30s — starting anyway",
    file=sys.stderr,
)
sys.exit(0)
