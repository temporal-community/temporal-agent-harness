"""Argument parsing for the ``temporal-agent-kit`` console script.

This module is only the command-line surface: it parses arguments, resolves the Temporal address,
and dispatches. The work lives in the modules the subcommands name — ``web.serve`` for the web
app process — so none of it is reachable only through a CLI.

Adding the harness to a project with the ``ui`` extra installs this command into the project's
environment (reachable as ``uv run temporal-agent-kit``), so a project gets the whole
web-facing harness with no repo checkout, no Node/pnpm, and no ``examples/`` tree.
A normal dev setup is:

    uv add 'temporal-agent-kit[ui]'
    uv run temporal-agent-kit serve path/to/agents.toml   # prebuilt Svelte UI + JSON API

``serve`` publishes the UI built into the wheel (``temporal_agent_kit/ui/dist``) over the agents
in its registry store, registering those of any ``agents.toml`` files it is given as it starts. It
starts sessions as the registry's agent workflows and lists them from Temporal visibility, so
beside Temporal it needs only the agents' workers. ``agents`` adds, lists, retires and deletes a
running server's agents through its HTTP API.

``schema`` needs no Temporal connection: it imports one agent class and prints its handlers and
declared state as JSON Schema (``harness.agent_schema``), the input to client codegen. With
``--protocol`` it prints the event stream's wire types instead, with ``--registry`` the schema
of an ``agents.toml`` registry, and with ``--registry-store`` the schema of the registry
store's Nexus operations. The running server exposes the HTTP contract at ``/openapi.json``.

``init`` copies a bundled starter; ``skills`` locates or installs this release's agent skill.
``doctor`` reads configuration and service status without creating or resetting workflows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from temporal_agent_kit.development import SKILL_DIRECTORIES, SKILL_TARGETS
from temporal_agent_kit.web.client import (
    DEFAULT_TEMPORAL_ADDRESS,
    TemporalConnection,
    ensure_temporal_connection,
)

TEMPORAL_CONNECTION_HELP = """\
Temporal connection:
  Service commands and doctor resolve connections with temporalio's standard client config, so the
  server and the agent workers land on the same namespace when configured the same way. In precedence order:

    1. --temporal-address HOST:PORT
         Overrides the ADDRESS only. Namespace, TLS, and API key still come from the
         profile below, so this composes with a temporal.toml rather than replacing it.
    2. TEMPORAL_ADDRESS (and the other TEMPORAL_* env overrides).
    3. The profile named by --temporal-profile, else TEMPORAL_PROFILE, else "default", in
         the temporal.toml named by --temporal-config-file, else TEMPORAL_CONFIG_FILE, else
         ~/.config/temporalio/temporal.toml.
    4. localhost:7233
         The `temporal server start-dev` default. Used ONLY when nothing above resolves
         an address, so it never overrides a configured profile.

  Note: a temporal.toml in the CURRENT DIRECTORY is NOT picked up automatically --
  temporalio reads only the user-level file above. Point at a project-local one
  explicitly:

      temporal-agent-kit serve --temporal-config-file temporal.toml ./agents.toml

Task queues:
  TASK_QUEUE_PREFIX, set to the same value for `serve` and every agent worker, moves the whole
  deployment onto its own queues: agents.toml's queues all become `<prefix>-<name>`, and the
  server lists only sessions on those queues.

Examples:
  temporal-agent-kit serve ./agents.toml
  temporal-agent-kit serve one/agents.toml two/agents.toml --port 9000
  temporal-agent-kit serve ./agents.toml --temporal-address my-host:7233

  # A project-local config file, and a named profile within it (e.g. Temporal Cloud,
  # whose namespace and API key come from that profile):
  temporal-agent-kit serve --temporal-config-file temporal.toml --temporal-profile cloud \\
      ./agents.toml
"""


def _serve(args: argparse.Namespace) -> None:
    # Imported at call time so `--help` on the other subcommand doesn't pay for FastAPI.
    from temporal_agent_kit.web.serve import run_server

    run_server(
        *(Path(path) for path in args.registry_paths),
        registry_store_endpoint=args.registry_store_endpoint,
        host=args.host,
        port=args.port,
    )


def _agents(args: argparse.Namespace) -> None:
    from temporal_agent_kit.web.agent_commands import (
        AgentCommandError,
        add_agents,
        delete_agent,
        format_agents,
        list_agents,
        retire_agent,
    )

    try:
        if args.action == "add":
            keys = add_agents(Path(args.registry_path), args.server)
            print("Registered " + ", ".join(keys))
        elif args.action == "list":
            print(format_agents(list_agents(args.server)))
        elif args.action == "retire":
            retire_agent(args.key, args.server)
            print(f"Retired {args.key}")
        else:
            delete_agent(args.key, args.server)
            print(f"Deleted {args.key}")
    except (AgentCommandError, OSError, ValueError) as e:
        raise SystemExit(f"temporal-agent-kit agents {args.action}: {e}") from None


def _schema(args: argparse.Namespace) -> None:
    from temporal_agent_kit.agent_schema import (
        dump_agent_schema,
        dump_protocol_schema,
        load_agent_class,
    )

    if [args.agent is not None, args.protocol, args.registry, args.registry_store].count(True) != 1:
        raise SystemExit(
            "temporal-agent-kit schema: give one of an agent class, --protocol, --registry or "
            "--registry-store"
        )
    try:
        if args.protocol:
            text = dump_protocol_schema()
        elif args.registry:
            from temporal_agent_kit.web.registry import dump_registry_schema

            text = dump_registry_schema()
        elif args.registry_store:
            from temporal_agent_kit.web.nexus_registry_store import dump_registry_store_schema

            text = dump_registry_store_schema()
        else:
            text = dump_agent_schema(load_agent_class(args.agent))
    except ValueError as e:
        raise SystemExit(f"temporal-agent-kit schema: {e}") from None
    if args.output is None:
        sys.stdout.write(text)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)


def _development(args: argparse.Namespace) -> None:
    from temporal_agent_kit.development import (
        SKILL_NAME,
        SKILL_PROMPTS,
        choose_skill_tools,
        create_starter,
        install_skill,
        package_version,
        skill_destinations,
        skill_path,
    )

    try:
        if args.command == "init":
            root = create_starter(Path(args.destination))
            print(f"Created {root}")
            print("Next: follow its README.md to install dependencies, add credentials, and start the services.")
        elif args.command == "skills" and args.action == "path":
            print(skill_path())
        elif args.command == "skills":
            release = package_version()
            tools = args.tools
            if not tools and args.dest is None and sys.stdin.isatty():
                tools = choose_skill_tools()
            for destination in skill_destinations(tools, args.dest):
                installed = install_skill(destination)
                state = "Installed" if installed.changed else "Already up to date:"
                print(f"{state} {SKILL_NAME} {release} in {installed.root}")
            prompts = ", ".join(f"`{prompt}` in {tool}" for tool, prompt in SKILL_PROMPTS.items())
            print(f"Next: ask your coding agent to use the skill ({prompts}).")
        else:
            from temporal_agent_kit.web.doctor import diagnose

            report = diagnose(Path(args.registry) if args.registry else None, args.api_url, args.timeout)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print("Versions: " + ", ".join(f"{k}={v}" for k, v in report["versions"].items()))
                for check in report["checks"]:
                    print(f"{'OK' if check['ok'] else 'FAIL'} {check['check']}: {check['detail']}")
            if not report["ok"]:
                raise SystemExit(1)
    except (OSError, ValueError) as error:
        raise SystemExit(f"temporal-agent-kit {args.command}: {error}") from None


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
    connection.add_argument(
        "--temporal-config-file",
        default=None,
        type=Path,
        metavar="FILE",
        help="The temporal.toml to read connection profiles from. Overrides "
        "TEMPORAL_CONFIG_FILE.",
    )
    connection.add_argument(
        "--temporal-profile",
        default=None,
        metavar="NAME",
        help="The profile in that file to connect with. Overrides TEMPORAL_PROFILE.",
    )

    parser = argparse.ArgumentParser(
        prog="temporal-agent-kit",
        description="Build and run Temporal agents: create a starter, install a coding-agent skill, "
        "export schemas, diagnose services, or run the shared API/UI.",
        epilog=TEMPORAL_CONNECTION_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    init = subparsers.add_parser("init", help="Copy the bundled OpenAI starter without overwriting files.")
    init.add_argument("destination")
    init.set_defaults(handler=_development)
    skills = subparsers.add_parser(
        "skills",
        help="Locate or install this SDK release's coding-agent skill.",
        description="`path` prints the bundled skill directory. `install` copies it into the "
        "project where your coding agents discover skills: Claude Code reads .claude/skills; Codex "
        "and other agents read the shared .agents/skills. Without --for or --dest it asks which to "
        "install for. Reruns update unchanged files and preserve local edits.",
    )
    skills.add_argument("action", choices=("path", "install"))
    skills.add_argument(
        "--for",
        dest="tools",
        action="append",
        choices=tuple(SKILL_DIRECTORIES),
        metavar="{" + ",".join(SKILL_DIRECTORIES) + "}",
        help="Install for these coding agents (repeatable): "
        + "; ".join(f"{k}: {SKILL_TARGETS[k]} ({v})" for k, v in SKILL_DIRECTORIES.items())
        + ".",
    )
    skills.add_argument("--dest", default=None, help="Parent directory for a tool that reads skills from somewhere else.")
    skills.set_defaults(handler=_development)
    doctor = subparsers.add_parser("doctor", parents=[connection], help="Read-only configuration and service diagnostics.")
    doctor.add_argument("--registry", help="Validate this TOML registry and its referenced schemas.")
    doctor.add_argument("--api-url", default="http://localhost:8000")
    doctor.add_argument("--timeout", type=float, default=5)
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_development)

    serve = subparsers.add_parser(
        "serve",
        parents=[connection],
        help="Serve the packaged web UI over the agents in its registry.",
        description="Serve the prebuilt Agent Kit web UI and JSON API over the agents in its "
        "registry store, registering the agents of any agents.toml files given as it starts. "
        "It starts sessions as the registry's agent workflows and lists them from Temporal "
        "visibility; run the agents' workers against the same namespace.",
        epilog=TEMPORAL_CONNECTION_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve.add_argument(
        "registry_paths",
        nargs="*",
        metavar="registry_path",
        help="agents.toml files whose agents to register as the server starts, all of them or "
        "none. Without any, the server serves the agents its registry already holds.",
    )
    serve.add_argument(
        "--registry-store-endpoint",
        default=None,
        metavar="NAME",
        help="Keep the agent registry in the AgentKitRegistryStore Nexus service behind the "
        "Nexus endpoint NAME, instead of the registry workflow. `temporal-agent-kit schema "
        "--registry-store` prints its operations' schema.",
    )
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(handler=_serve)

    agents = subparsers.add_parser(
        "agents",
        help="Add, list, retire or delete a running server's agents.",
        description="Change a running server's agent registry through its HTTP API. `add` "
        "registers every agent of an agents.toml file, all or none, with the schema files its "
        "entries name; registering a key again replaces it and makes it active. `retire` stops "
        "an agent from starting new sessions but keeps its sessions listed; `delete` removes "
        "it, and its sessions leave the session list.",
    )
    agents_actions = agents.add_subparsers(dest="action", metavar="<action>", required=True)
    agents_add = agents_actions.add_parser("add", help="Register the agents of an agents.toml.")
    agents_add.add_argument("registry_path", metavar="agents.toml")
    agents_actions.add_parser("list", help="List the registered agents.")
    for action, help_text in (
        ("retire", "Stop an agent from starting new sessions."),
        ("delete", "Remove an agent from the registry."),
    ):
        agents_key = agents_actions.add_parser(action, help=help_text)
        agents_key.add_argument("key")
    for action_parser in agents_actions.choices.values():
        action_parser.add_argument(
            "--server",
            default="http://localhost:8000",
            metavar="URL",
            help="The Agent Kit server (default: http://localhost:8000).",
        )
    agents.set_defaults(handler=_agents)

    schema = subparsers.add_parser(
        "schema",
        help="Print an agent's handlers and declared state, the event protocol, or the "
        "agents.toml format, as JSON Schema.",
        description="Import one agent class and print its @agent.accepts handlers (input "
        "and output models) and its agent.state(...) declarations as a single JSON Schema "
        "document with shared $defs — the input to client codegen. Starts no workflow and "
        "needs no Temporal connection.",
    )
    schema.add_argument(
        "agent",
        nargs="?",
        metavar="module.path:ClassName",
        help="The agent class, e.g. examples.tictactoe.workflow:TicTacToeAgentWorkflow.",
    )
    schema.add_argument(
        "--protocol",
        action="store_true",
        help="Print the event stream's wire types (every agent publishes the same ones) "
        "instead of one agent's schema.",
    )
    schema.add_argument(
        "--registry",
        action="store_true",
        help="Print the schema of an agents.toml registry instead of one agent's schema.",
    )
    schema.add_argument(
        "--registry-store",
        action="store_true",
        help="Print the schema of the AgentKitRegistryStore Nexus service's operations instead "
        "of one agent's schema.",
    )
    schema.add_argument(
        "-o", "--output", default=None, help="Write to this file instead of stdout."
    )
    schema.set_defaults(handler=_schema)

    # A bare `temporal-agent-kit` is how someone finds out what this command does, so answer
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

    if hasattr(args, "temporal_address"):
        config_file: Path | None = args.temporal_config_file
        if config_file is not None and not config_file.is_file():
            parser.error(f"--temporal-config-file: no file {config_file}")
        ensure_temporal_connection(
            TemporalConnection(
                address=args.temporal_address,
                config_file=config_file,
                profile=args.temporal_profile,
            )
        )
    handler(args)


if __name__ == "__main__":
    main()
