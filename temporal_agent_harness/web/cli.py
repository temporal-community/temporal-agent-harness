"""Argument parsing for the ``temporal-agent-harness`` console script.

This module is only the command-line surface: it parses arguments, resolves the Temporal address,
and dispatches. The work lives in the modules the subcommands name — ``web.serve`` for the web
app process and ``web.session_manager_worker`` for the session-manager worker — so neither is reachable only
through a CLI.

Adding the harness to a project with the ``ui`` extra installs this command into the project's
environment (reachable as ``uv run temporal-agent-harness``), so a project gets the whole
web-facing harness with no repo checkout, no Node/pnpm, and no ``examples/`` tree.
A normal dev setup runs both subcommands:

    uv add 'temporal-agent-harness[ui]'
    uv run temporal-agent-harness session-manager &          # hosts SessionManagerWorkflow
    uv run temporal-agent-harness serve path/to/agents.toml   # prebuilt Svelte UI + JSON API

``serve`` publishes the UI built into the wheel (``temporal_agent_harness/ui/dist``) over one or
more ``agents.toml`` registries. ``session-manager`` runs the agent-agnostic worker that hosts
the packaged ``SessionManagerWorkflow``; without it the server comes up but ``/api/agents`` and
session creation have nothing to answer them, since both are workflow queries.
"""

from __future__ import annotations

import argparse
import sys

from temporal_agent_harness.web.client import (
    DEFAULT_TEMPORAL_ADDRESS,
    ensure_temporal_address,
)

TEMPORAL_CONNECTION_HELP = """\
Temporal connection:
  Every subcommand resolves its connection with temporalio's standard client config, so the
  server and the session-manager worker land on the same namespace when configured the same
  way. In precedence order:

    1. --temporal-address HOST:PORT
         Overrides the ADDRESS only. Namespace, TLS, and API key still come from the
         profile below, so this composes with a temporal.toml rather than replacing it.
    2. TEMPORAL_ADDRESS (and the other TEMPORAL_* env overrides).
    3. The profile named by TEMPORAL_PROFILE (default: "default") in the temporal.toml
         named by TEMPORAL_CONFIG_FILE, else ~/.config/temporalio/temporal.toml.
    4. localhost:7233
         The `temporal server start-dev` default. Used ONLY when nothing above resolves
         an address, so it never overrides a configured profile.

  Note: a temporal.toml in the CURRENT DIRECTORY is NOT picked up automatically --
  temporalio reads only the user-level file above. Point at a project-local one
  explicitly:

      TEMPORAL_CONFIG_FILE=./temporal.toml temporal-agent-harness serve ./agents.toml

Examples:
  temporal-agent-harness session-manager
  temporal-agent-harness serve ./agents.toml
  temporal-agent-harness serve one/agents.toml two/agents.toml --port 9000
  temporal-agent-harness serve ./agents.toml --temporal-address my-host:7233

  # A project-local config file, and a named profile within it (e.g. Temporal Cloud,
  # whose namespace and API key come from that profile):
  TEMPORAL_CONFIG_FILE=./temporal.toml TEMPORAL_PROFILE=cloud \\
      temporal-agent-harness serve ./agents.toml
"""


def _serve(args: argparse.Namespace) -> None:
    # Imported at call time so `--help` on the other subcommand doesn't pay for FastAPI.
    from temporal_agent_harness.web.serve import run_server

    run_server(*args.registry_paths, host=args.host, port=args.port)


def _session_manager(args: argparse.Namespace) -> None:
    from temporal_agent_harness.web.session_manager_worker import run_session_manager_worker

    run_session_manager_worker(task_queue=args.task_queue, identity=args.identity)


def main(argv: list[str] | None = None) -> None:
    # --temporal-address is shared rather than duplicated, so the two subcommands can never
    # drift on the one setting that has to agree between them.
    connection = argparse.ArgumentParser(add_help=False)
    connection.add_argument(
        "--temporal-address",
        default=None,
        metavar="HOST:PORT",
        help="Temporal frontend address. Overrides any temporal.toml profile / "
        "TEMPORAL_ADDRESS. When omitted and nothing else configures one, defaults to "
        f"{DEFAULT_TEMPORAL_ADDRESS}.",
    )

    parser = argparse.ArgumentParser(
        prog="temporal-agent-harness",
        description="Run the packaged Temporal agent harness: the web UI over your "
        "agents.toml registries, and the session-manager worker they need.",
        epilog=TEMPORAL_CONNECTION_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    serve = subparsers.add_parser(
        "serve",
        parents=[connection],
        help="Serve the packaged web UI over one or more agents.toml registries.",
        description="Serve the prebuilt harness web UI and JSON API over one or more "
        "agents.toml registries. Needs `temporal-agent-harness session-manager` running "
        "against the same namespace, since the agent list and session creation are "
        "workflow queries.",
        epilog=TEMPORAL_CONNECTION_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve.add_argument(
        "registry_paths",
        nargs="+",
        metavar="registry_path",
        help="Path(s) to agents.toml registries. One serves that registry standalone; "
        "several are merged so the UI lists all their agents.",
    )
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=_serve)

    session_manager = subparsers.add_parser(
        "session-manager",
        parents=[connection],
        help="Run the worker hosting the packaged session-manager workflow.",
        description="Run the agent-agnostic worker that hosts the packaged "
        "SessionManagerWorkflow. It registers no agent workflows or activities of its own — "
        "it launches whichever agents `serve` registered, as child workflows on their own "
        "task queues — so one worker serves every agent.",
        epilog=TEMPORAL_CONNECTION_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Imported lazily: reaching for the default task queue shouldn't drag the session-manager
    # module into `--help` on the other subcommand.
    from temporal_agent_harness.web.session_manager import SESSION_MANAGER_TASK_QUEUE

    session_manager.add_argument(
        "--task-queue",
        default=SESSION_MANAGER_TASK_QUEUE,
        help="Task queue to poll. Must match what the server expects "
        "(default: %(default)r).",
    )
    session_manager.add_argument(
        "--identity",
        default=None,
        help="Worker identity reported to Temporal. Defaults to the SDK's own identity.",
    )
    session_manager.set_defaults(handler=_session_manager)

    # A bare `temporal-agent-harness` is how someone finds out what this command does, so answer
    # with the full help (subcommands and connection rules included) rather than argparse's terse
    # "the following arguments are required" one-liner. Still exits non-zero: the invocation
    # really was incomplete, and a script shouldn't read it as success.
    if not (sys.argv[1:] if argv is None else argv):
        parser.print_help()
        raise SystemExit(2)

    args = parser.parse_args(argv)

    # Flags without a subcommand name no action to take, so answer them the same way.
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        raise SystemExit(2)

    ensure_temporal_address(args.temporal_address)
    handler(args)


if __name__ == "__main__":
    main()
