"""Host Codex Hello with a workspace and permissions chosen by the operator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import shutil
import subprocess

from temporalio.client import Client
from temporalio.envconfig import ClientConfig
from temporalio.worker import Worker

from temporal_agent_harness.ai_sdks.codex import CodexConfig, CodexPlugin

from .workflow import TASK_QUEUE, CodexHelloAgentWorkflow


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=REPOSITORY_ROOT,
        help="Repository Codex may inspect (default: this harness checkout)",
    )
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument("--model", help="Optional Codex model override")
    parser.add_argument(
        "--state-dir", type=Path, default=REPOSITORY_ROOT / ".codex-runs",
        help="Worker-local directory for Codex run diagnostics",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--task-queue", default=os.environ.get("CODEX_HELLO_TASK_QUEUE", TASK_QUEUE),
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check the workspace, CLI, and saved login without contacting Temporal or a model",
    )
    args = parser.parse_args()
    args.workspace = args.workspace.expanduser().resolve()
    args.state_dir = args.state_dir.expanduser().resolve()
    if not args.workspace.is_dir():
        parser.error(f"workspace is not a directory: {args.workspace}")
    if not 1 <= args.timeout_seconds <= 3600:
        parser.error("--timeout-seconds must be between 1 and 3600")
    binary = shutil.which(os.path.expanduser(args.codex_binary))
    if binary is None:
        parser.error(f"Codex CLI not found: {args.codex_binary}; install it and run codex login")
    args.codex_binary = binary
    return args


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    config = CodexConfig(
        workspace=str(args.workspace),
        sandbox="read-only",
        codex_binary=args.codex_binary,
        model=args.model,
        state_dir=str(args.state_dir),
        timeout_seconds=args.timeout_seconds,
    )
    if args.check:
        try:
            result = subprocess.run(
                [args.codex_binary, "login", "status"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemExit(f"Could not check Codex login: {exc}") from exc
        if result.returncode:
            raise SystemExit("Codex has no saved login. Run codex login, then repeat --check.")
        print(f"Codex Hello ready: workspace={args.workspace} sandbox=read-only; login present")
        return

    connect_config = ClientConfig.load_client_connect_config()
    connect_config.setdefault("target_host", "localhost:7233")
    # CodexPlugin supplies the data converter and registers its CLI activity on
    # workers created from this client. No activities or model SDK are wired here.
    client = await Client.connect(**connect_config, plugins=[CodexPlugin(config)])
    worker = Worker(
        client,
        task_queue=args.task_queue,
        workflows=[CodexHelloAgentWorkflow],
        activities=[],
    )
    print(
        f"Codex Hello worker ready: address={connect_config['target_host']} "
        f"namespace={connect_config.get('namespace', 'default')} "
        f"taskQueue={args.task_queue} workspace={args.workspace} sandbox=read-only",
        flush=True,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
