"""One local process supervisor; owns only the dev server it starts."""

import argparse
import fcntl
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Start boltzmann, your local development app")
    parser.add_argument("--data-dir", default="~/.local/share/sdlc-builder")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--temporal-port", type=int, default=7333)
    parser.add_argument(
        "--external-temporal",
        help="Use an existing server (host:port), without starting or stopping it",
    )
    args = parser.parse_args()
    root = Path(args.data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["SDLC_DATA_DIR"] = str(root)
    os.umask(0o077)
    lock = (root / "app.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "Another SDLC app is using this data directory.\n")
    token_file = root / "session-token"
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(32))
    token_file.chmod(0o600)
    server = None
    log = None
    try:
        if args.external_temporal:
            os.environ["TEMPORAL_ADDRESS"] = args.external_temporal
        else:
            executable = shutil.which("temporal")
            if not executable:
                parser.exit(
                    1,
                    "Install the Temporal CLI first (macOS: brew install temporal).\n",
                )
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", args.temporal_port)) == 0:
                    parser.exit(
                        1,
                        "Temporal port is occupied. Choose --temporal-port or explicitly use --external-temporal.\n",
                    )
            os.environ["TEMPORAL_ADDRESS"] = f"127.0.0.1:{args.temporal_port}"
            log = (root / "temporal.log").open("a")
            server = subprocess.Popen(
                [
                    executable,
                    "server",
                    "start-dev",
                    "--ip",
                    "127.0.0.1",
                    "--port",
                    str(args.temporal_port),
                    "--headless",
                    "--db-filename",
                    str(root / "temporal.sqlite3"),
                    "--disable-config-env",
                    "--disable-config-file",
                ],
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                # Ctrl-C targets the launcher's foreground process group. Keep
                # Temporal alive until the worker has drained, then stop it in
                # finally below; otherwise SDK worker shutdown can hang.
                start_new_session=True,
            )
            for _ in range(100):
                if server.poll() is not None:
                    parser.exit(
                        1, f"Temporal could not start. See {root / 'temporal.log'}\n"
                    )
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", args.temporal_port)) == 0:
                        break
                time.sleep(0.1)
            else:
                parser.exit(
                    1, f"Timed out starting Temporal. See {root / 'temporal.log'}\n"
                )
        print(
            f"\nboltzmann · Coding agent V2\nOpen http://127.0.0.1:{args.port}/#token={token_file.read_text().strip()}\n"
            f"Data: {root}\n"
            f"Temporal: {os.environ['TEMPORAL_ADDRESS']} ({'external; keep it running' if args.external_temporal else 'started automatically'})\n"
            "Keep this terminal running. Ctrl-C stops the app; tasks resume on the next launch.\n",
            flush=True,
        )
        import uvicorn

        from .server import create_app

        uvicorn.run(
            create_app(),
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
        )
    finally:
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        if log:
            log.close()
        lock.close()


if __name__ == "__main__":
    main()
