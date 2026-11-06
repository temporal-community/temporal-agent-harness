"""Schema-driven Temporal CLI extension using the Harness client contract."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
import tomllib
import uuid
from contextlib import aclosing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.envconfig import ClientConfig
from temporalio.common import WorkflowIDConflictPolicy

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    AgentConfig,
    AgentMessageReply,
    CallbackRequested,
    PendingApproval,
    PendingCallback,
    ToolApprovalRequested,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.web.registry import AgentRegistryEntry, AgentRegistryFile

SESSION_COMMANDS = {
    "approve": "approve a pending tool call",
    "callback": "submit a callback result or failure",
    "close": "close a session",
    "deny": "deny a pending tool call",
    "follow": "stream a session's progress and replies",
    "status": "show session status and pending approvals or callbacks",
}


def default_registry() -> Path:
    local = Path("agents.toml")
    if local.is_file():
        return local
    saved = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "temporal-agent/agents.toml"
    )
    return saved if saved.is_file() else local


def interactive_output(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "output", "text") == "text"
        and sys.stdout.isatty()
        and sys.stderr.isatty()
        and os.environ.get("TERM") != "dumb"
        and args.command not in ("approve", "callback", "close", "deny", "status")
        and not getattr(args, "detach", False)
        and not getattr(args, "dry_run", False)
    )


def load_schema(path: Path, entry: AgentRegistryEntry) -> dict[str, Any]:
    if not entry.schema_path:
        raise ValueError(
            f"{entry.key}: set schema in {path} to the generated schema document"
        )
    schema = path.parent / entry.schema_path
    if not schema.is_file():
        raise ValueError(
            f"{entry.key}: supply the generated schema document at {schema}"
        )
    result = json.loads(schema.read_text())
    if result["agent"] != entry.workflow_type:
        raise ValueError(f"{entry.key}: schema and registry identity disagree")
    return result


def resolve_schema(
    document: dict[str, Any], definition: dict[str, Any]
) -> dict[str, Any]:
    seen = set()
    while "$ref" in definition:
        reference = definition["$ref"]
        if reference in seen or not reference.startswith("#/$defs/"):
            raise ValueError(f"Cannot generate flags for schema reference: {reference}")
        seen.add(reference)
        definition = document["$defs"][reference.removeprefix("#/$defs/")]
    return definition


def thought_text(delta: dict[str, Any]) -> str | None:
    """The text fragment of a provider's thought delta (Gemini, OpenAI, Pydantic AI)."""
    content = delta.get("content")
    if isinstance(content, dict):
        content = content.get("text")
    for fragment in (content, delta.get("content_delta"), delta.get("delta")):
        if isinstance(fragment, str):
            return fragment
    return None


def json_value(value: str) -> Any:
    return json.loads(Path(value[1:]).read_text() if value.startswith("@") else value)


def add_fields(
    parser: argparse.ArgumentParser,
    doc: dict[str, Any],
    definition: dict[str, Any] | None,
    group: str,
    prefix: str = "",
) -> None:
    definition = resolve_schema(doc, definition or {"type": "null"})
    if definition.get("type") != "object":
        return
    for name, field in definition.get("properties", {}).items():
        field = resolve_schema(doc, field)
        branches = [
            part for part in field.get("anyOf", []) if part.get("type") != "null"
        ]
        shape = resolve_schema(doc, branches[0]) if len(branches) == 1 else field
        option = "--" + prefix + name.replace("_", "-")
        while option in parser._option_string_actions or (
            shape.get("type") == "boolean"
            and "--no-" + option[2:] in parser._option_string_actions
        ):
            option = "--arg-" + option[2:]
        options: dict[str, Any] = {
            "dest": f"{group}:{name}",
            "default": argparse.SUPPRESS,
            "help": field.get("description", field.get("title", name)),
        }
        if name in definition.get("required", []):
            options["help"] += " (required)"
        elif "default" in field:
            options["help"] += (
                f" (default: {json.dumps(field['default'], ensure_ascii=False)})"
            )
        if shape.get("type") in ("array", "object"):
            options["help"] += "; JSON or @file"
        if shape.get("type") == "boolean":
            options["action"] = argparse.BooleanOptionalAction
        else:
            options["metavar"] = name.upper()
            options["type"] = {
                "string": str,
                "integer": int,
                "number": float,
            }.get(shape.get("type"), json_value)
        parser.add_argument(option, **options)


def field_values(args: argparse.Namespace, group: str) -> dict[str, Any]:
    return {
        key.split(":", 1)[1]: value
        for key, value in vars(args).items()
        if key.startswith(group + ":")
    }


def validate(doc: dict[str, Any], definition: dict[str, Any], value: Any) -> None:
    try:
        from jsonschema import Draft202012Validator, ValidationError
    except ImportError:
        raise ValueError(
            "Install temporal-agent-harness[cli] to validate agent inputs"
        ) from None

    try:
        Draft202012Validator({"$defs": doc.get("$defs", {}), **definition}).validate(
            value
        )
    except ValidationError as error:
        location = ".".join(str(part) for part in error.absolute_path) or "input"
        raise ValueError(f"{location}: {error.message}") from None


def parse_args(argv: list[str]) -> argparse.Namespace:
    common = argparse.ArgumentParser(
        add_help=False, allow_abbrev=False, argument_default=argparse.SUPPRESS
    )
    common.add_argument("--registry", type=Path, help="Harness agents.toml")
    common.add_argument("--profile", help="Temporal configuration profile")
    common.add_argument(
        "--address",
        help="Temporal host:port (default: configured address or localhost:7233)",
    )
    common.add_argument(
        "--namespace", help="Temporal namespace (overrides the profile)"
    )
    common.add_argument(
        "--output",
        choices=("text", "json"),
        help="output format (default: text); JSON results go to stdout",
    )
    common.add_argument(
        "--timeout", type=float, help="seconds to wait (default: unlimited)"
    )
    known, remaining = common.parse_known_args(argv)
    path = getattr(known, "registry", default_registry())
    descriptors = []
    try:
        if path.exists() or hasattr(known, "registry"):
            with path.open("rb") as file:
                descriptors = AgentRegistryFile.model_validate(
                    tomllib.load(file)
                ).agents
        seen_keys = set()
        for descriptor in descriptors:
            if descriptor.key in seen_keys:
                raise ValueError(f"Duplicate agent key {descriptor.key!r} in {path}.")
            seen_keys.add(descriptor.key)
            if descriptor.key in SESSION_COMMANDS:
                raise ValueError(
                    f"Agent name conflicts with a session command: {descriptor.key}"
                )
    except (OSError, ValueError):
        if remaining not in (["--help"], ["-h"]):
            raise
        descriptors = []
    root = argparse.ArgumentParser(
        prog="temporal agent",
        parents=[common],
        allow_abbrev=False,
        description="Run agents and follow their progress and results.",
        epilog="Agents are read from ./agents.toml or the saved user registry; use --registry PATH to select one. "
        "Connection settings come from the Temporal SDK's temporal.toml profile and TEMPORAL_* environment variables.",
    )
    commands = root.add_subparsers(dest="command", required=True)
    for descriptor in descriptors:
        agent = commands.add_parser(
            descriptor.key,
            parents=[common],
            help=" ".join(descriptor.description.split()),
            allow_abbrev=False,
        )
        if not remaining or remaining[0] != descriptor.key:
            continue
        doc = load_schema(path, descriptor)
        operations = doc["handlers"]
        methods = agent.add_subparsers(required=True) if len(operations) > 1 else None
        for name, operation in operations.items():
            target = agent
            if methods is not None:
                target = methods.add_parser(
                    name.replace("_", "-"),
                    parents=[common],
                    help=operation["description"],
                    allow_abbrev=False,
                )
            target.set_defaults(descriptor=descriptor, document=doc, handler=name)
            target.add_argument("--session", help="send to an existing session")
            target.add_argument("--request-id", help="idempotency key for this message")
            target.add_argument(
                "--detach",
                action="store_true",
                help="return after the message is accepted",
            )
            target.add_argument(
                "--dry-run",
                action="store_true",
                help="validate inputs without connecting",
            )
            target.add_argument(
                "--interactive",
                action="store_true",
                help="keep the session open for a conversation",
            )
            target.add_argument(
                "--input",
                type=json_value,
                default=argparse.SUPPRESS,
                help="complete message as JSON or @file",
            )
            target.add_argument(
                "--init",
                type=json_value,
                default=argparse.SUPPRESS,
                help="complete initialization data as JSON or @file",
            )
            add_fields(target, doc, operation["input"], "message")
            if doc.get("init_data"):
                add_fields(target, doc, doc["init_data"]["data"], "init", "init-")
    for action, description in SESSION_COMMANDS.items():
        command = commands.add_parser(
            action,
            parents=[common],
            allow_abbrev=False,
            help=description,
            description=description,
        )
        command.add_argument("session", help="session workflow ID")
        if action in ("approve", "deny", "callback"):
            command.add_argument("tool_id", help="tool ID from the pending request")
        if action in ("approve", "deny"):
            command.add_argument("--reason", help="reason for the approval decision")
        if action == "callback":
            result = command.add_mutually_exclusive_group(required=True)
            result.add_argument(
                "--result", type=json_value, help="callback result as JSON or @file"
            )
            result.add_argument(
                "--error", help="report a callback failure instead of a result"
            )
    args = root.parse_args(argv)
    args.registry = path.resolve()
    if getattr(args, "timeout", 1) <= 0:
        root.error("--timeout must be positive")
    return args


def emit(args: argparse.Namespace, kind: str, data: dict[str, Any]) -> None:
    if ui := getattr(args, "_ui", None):
        ui.emit(kind, data, getattr(args, "_reply_text", None))
        return
    output = getattr(args, "output", "text")
    chunk = kind == "progress" and data.get("event") in (
        "reply_delta",
        "thought_summary",
    )
    if getattr(args, "_text_open", False) and not chunk:
        print(file=sys.stderr)
        args._text_open = False
    if output == "json" and kind != "progress":
        text = json.dumps(data, indent=2)
    elif kind in ("result", "history") and "result" in data and "error" not in data:
        response = data["result"]
        if response.keys() == {"text"} and isinstance(response["text"], str):
            text = response["text"]
            if kind == "result" and text == getattr(args, "_reply_text", None):
                return
        else:
            text = json.dumps(response, indent=2, ensure_ascii=False)
    else:
        text = data["text"] if "text" in data else json.dumps(data, indent=2)
    print(
        text,
        file=sys.stderr if kind == "progress" else sys.stdout,
        end="" if chunk else "\n",
        flush=True,
    )
    args._text_open = chunk


async def read_line(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    loop = asyncio.get_running_loop()
    descriptor = sys.stdin.fileno()
    line = bytearray()
    try:
        while True:
            ready = loop.create_future()
            loop.add_reader(descriptor, lambda: ready.done() or ready.set_result(None))
            await ready
            # Leave pasted follow-ups on the descriptor for the next prompt.
            chunk = os.read(descriptor, 1)
            if chunk:
                line.extend(chunk)
            if not chunk or chunk == b"\n":
                return line.decode(sys.stdin.encoding or "utf-8")
    finally:
        loop.remove_reader(descriptor)


def connection_flags(args: argparse.Namespace) -> list[str]:
    flags = []
    for name in ("profile", "address", "namespace"):
        if (value := getattr(args, name, None)) is not None:
            flags.extend((f"--{name}", str(value)))
    return flags


def resume_command(
    args: argparse.Namespace, payload: dict[str, Any], input_field: str
) -> str:
    executable = Path(sys.executable).with_name("temporal-agent")
    installed = shutil.which("temporal-agent")
    if (
        installed
        and Path(installed).resolve() == executable.resolve()
        and shutil.which("temporal")
    ):
        command = ["temporal", "agent"]
    elif executable.is_file():
        command = [str(executable)]
    else:
        command = [sys.executable, "-m", "temporal_agent_harness.agent_cli"]
    command.append(args.command)
    if len(args.document["handlers"]) > 1:
        command.append(args.handler.replace("_", "-"))
    command += ["--registry", str(args.registry)]
    command += ["--session", args.session, "--interactive"]
    command += connection_flags(args)
    if (timeout := getattr(args, "timeout", None)) is not None:
        command += ["--timeout", str(timeout)]
    remaining = {key: value for key, value in payload.items() if key != input_field}
    if remaining:
        command += ["--input", json.dumps(remaining, ensure_ascii=False)]
    return shlex.join(command)


async def request_approval(
    client: AgentClient,
    event: PendingApproval | ToolApprovalRequested,
    args: argparse.Namespace,
) -> None:
    details = "\n\n".join(
        f"{name}:\n{value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)}"
        for name, value in event.tool_input.items()
    )
    emit(
        args,
        "progress",
        {
            "text": f"Approve {event.tool_name}?\n{details}",
            "event": "tool_approval_requested",
            "tool_name": event.tool_name,
            "tool_input": event.tool_input,
        },
    )
    if not sys.stdin.isatty():
        command = shlex.join(
            [
                "temporal",
                "agent",
                "approve",
                client.workflow_id,
                event.tool_id,
                *connection_flags(args),
            ]
        )
        raise ValueError(f"Approval required: {command} (or deny)")
    answer = await read_line("Approve once? [y/N] ")
    await client.approve_tool(
        event.tool_id, approved=answer.strip().lower() in ("y", "yes")
    )


async def request_callback(
    client: AgentClient,
    event: PendingCallback | CallbackRequested,
    args: argparse.Namespace,
) -> None:
    command = shlex.join(
        [
            "temporal",
            "agent",
            "callback",
            client.workflow_id,
            event.tool_id,
            "--result",
            "@result.json",
            *connection_flags(args),
        ]
    )
    instructions = f"Callback required: {command}"
    question = event.tool_input.get("question")
    options = event.tool_input.get("options")
    if not (
        getattr(args, "output", "text") == "text"
        and sys.stdin.isatty()
        and event.tool_name == "ask_user"
        and isinstance(question, str)
        and event.output_schema.get("type") == "string"
        and (
            options is None
            or isinstance(options, list)
            and all(isinstance(option, str) for option in options)
        )
    ):
        raise ValueError(instructions)
    text = question
    if options:
        text += "\n" + "\n".join(f"  • {option}" for option in options)
    emit(args, "progress", {"text": text})
    while True:
        answer = await read_line("Your answer (Ctrl+D to leave): ")
        if not answer:
            raise ValueError(instructions)
        answer = answer.rstrip("\r\n")
        if answer.strip():
            await client.provide_callback_result(event.tool_id, result=answer)
            return


async def follow(
    temporal: Client,
    client: AgentClient,
    args: argparse.Namespace,
    receipt: AgentMessageReply | None = None,
    *,
    history_field: str | None = None,
) -> dict[str, Any]:
    if (ui := getattr(args, "_ui", None)) and receipt is None and history_field is None:
        ui.start(client.workflow_id)
    status = await client.get_status()
    clients = {status.agent_id: client}
    statuses = [(client, status)]
    # Resuming past a child dispatch omits its replay, including unresolved gates.
    for owner, current in statuses:
        for event in current.pending_approvals:
            await request_approval(owner, event, args)
        for event in current.pending_callbacks:
            await request_callback(owner, event, args)
        for child in current.subagents:
            if child.subagent_id not in clients:
                child_client = AgentClient(temporal, child.workflow_id)
                clients[child.subagent_id] = child_client
                statuses.append((child_client, await child_client.get_status()))
    stream = await client.attach(
        on_item=lambda event, _: event,
        from_offset=receipt.accepted_offset if receipt else 0,
    )
    result = {"session": client.workflow_id}
    if receipt:
        result["receipt"] = asdict(receipt)
    root_agent = status.agent_id
    replies = {}
    async with aclosing(stream):
        async for envelope in stream:
            event = envelope.event
            own = envelope.agent_id == root_agent and (
                receipt is None or envelope.message_id == receipt.message_id
            )
            kind = event.type
            text = None
            if kind == "subagent_started":
                clients[event.subagent_id] = AgentClient(temporal, event.workflow_id)
            elif kind == "message_handler_end" and own:
                result.pop("error", None)
                result["result"] = event.output
                args._reply_text = "".join(replies.pop(envelope.message_id, []))
            elif kind == "message_handler_error" and own:
                result.pop("result", None)
                replies.pop(envelope.message_id, None)
                result["error"] = event.message
            elif kind == "tool_approval_requested":
                owner = clients[envelope.agent_id]
                pending = await owner.get_pending_approvals()
                if any(item.tool_id == event.tool_id for item in pending):
                    await request_approval(owner, event, args)
            elif kind == "callback_requested":
                owner = clients[envelope.agent_id]
                pending = await owner.get_pending_callbacks()
                if any(item.tool_id == event.tool_id for item in pending):
                    await request_callback(owner, event, args)
            elif kind == "reply_delta":
                text = event.text
                if own and getattr(args, "output", "text") == "text":
                    replies.setdefault(envelope.message_id, []).append(text)
            elif kind == "thought_summary":
                text = thought_text(event.delta)
            elif kind in ("tool_start", "tool_end", "tool_error"):
                text = f"{kind}: {event.tool_name}"
            elif kind in ("message_handler_start", "model_interaction_started"):
                text = "Working…" if kind == "message_handler_start" else "Thinking…"
            if history_field is not None:
                if own and kind == "message_accepted":
                    prompt = event.payload.get(history_field)
                    emit(
                        args,
                        "user",
                        {
                            "text": prompt
                            if event.handler == args.handler and isinstance(prompt, str)
                            else json.dumps(event.payload, ensure_ascii=False)
                        },
                    )
                elif own and kind in ("message_handler_end", "message_handler_error"):
                    emit(
                        args,
                        "history",
                        {"result": event.output}
                        if kind == "message_handler_end"
                        else {"error": event.message},
                    )
            elif text is not None:
                emit(
                    args,
                    "progress",
                    {
                        "text": text,
                        "event": kind,
                        "agent": envelope.agent_id,
                        "subagent": envelope.agent_id != root_agent,
                        **(
                            {"tool_id": event.tool_id}
                            if kind in ("tool_start", "tool_end", "tool_error")
                            else {}
                        ),
                    },
                )
            if (
                receipt
                and envelope.agent_id == root_agent
                and envelope.turn_id == receipt.turn_id
                and kind == "turn_end"
            ):
                break
    if receipt and "result" not in result and "error" not in result:
        raise ValueError("The stream ended without a response for this message")
    return result


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    existing = bool(args.session)
    interactive = getattr(args, "interactive", False)
    timeout = getattr(args, "timeout", None)
    ui = getattr(args, "_ui", None)
    input_field = None
    initial = None
    if args.command not in SESSION_COMMANDS:
        doc, handler = args.document, args.handler
        payload, init = field_values(args, "message"), field_values(args, "init")
        for field, flags in (("input", payload), ("init", init)):
            if hasattr(args, field) and flags:
                raise ValueError(f"Use either --{field} or individual fields")
        payload = getattr(args, "input", payload)
        has_init = hasattr(args, "init") or bool(init)
        init = getattr(args, "init", init)
        message_schema = doc["handlers"][handler]["input"]
        if interactive:
            if args.detach or args.dry_run:
                raise ValueError(
                    "--interactive cannot be combined with --detach or --dry-run"
                )
            if getattr(args, "output", "text") != "text" or not sys.stdin.isatty():
                raise ValueError(
                    "--interactive requires terminal input and text output"
                )
            definition = resolve_schema(doc, doc["handlers"][handler]["input"])
            fields = [
                name
                for name in definition.get("required", [])
                if resolve_schema(
                    doc, definition.get("properties", {}).get(name, {})
                ).get("type")
                == "string"
            ]
            if len(fields) != 1:
                raise ValueError(
                    "--interactive requires exactly one required string input field"
                )
            input_field = fields[0]
            if isinstance(payload, dict) and input_field not in payload:
                message_schema = {
                    **definition,
                    "required": [
                        name
                        for name in definition.get("required", [])
                        if name != input_field
                    ],
                }
        validate(doc, message_schema, payload)
        if has_init and args.session:
            raise ValueError("Initialization fields apply only to a new session")
        if has_init and not doc.get("init_data"):
            raise ValueError("This agent takes no initialization data")
        if not args.session and doc.get("init_data"):
            if has_init or doc["init_data"]["required"]:
                validate(doc, doc["init_data"]["data"], init)
                initial = init
        if args.dry_run:
            return {
                "agent": args.descriptor.key,
                "handler": handler,
                "input": payload,
                "init_data": initial,
                "session": args.session,
            }
    config = ClientConfig.load_client_connect_config(
        profile=getattr(args, "profile", None)
    )
    for flag, key in (("address", "target_host"), ("namespace", "namespace")):
        if value := getattr(args, flag, None):
            config[key] = value
    config.setdefault("target_host", "localhost:7233")
    async with asyncio.timeout(timeout if interactive else None):
        temporal = await Client.connect(**config, plugins=[AgentHarnessPlugin()])
        session = args.session or (
            f"{args.descriptor.key}-"
            f"{uuid.uuid5(uuid.NAMESPACE_URL, args.descriptor.key + ':' + args.request_id)}"
        )
        args.session = session
        client = AgentClient(temporal, session)
        if args.command == "status":
            status = await client.get_status()
            return {
                "session": session,
                "status": TypeAdapter(type(status)).dump_python(status, mode="json"),
            }
        if args.command == "close":
            await temporal.get_workflow_handle(session).signal("close")
            return {"session": session, "close_requested": True}
        if args.command in ("approve", "deny"):
            await client.approve_tool(
                args.tool_id, approved=args.command == "approve", reason=args.reason
            )
            return {
                "session": session,
                "tool_id": args.tool_id,
                "approved": args.command == "approve",
            }
        if args.command == "callback":
            await client.provide_callback_result(
                args.tool_id,
                result=getattr(args, "result", None),
                error=getattr(args, "error", None),
            )
            return {"session": session, "tool_id": args.tool_id, "accepted": True}
        if args.command == "follow":
            return await follow(temporal, client, args)

        async def check_session() -> None:
            description = await temporal.get_workflow_handle(session).describe()
            if description.workflow_type != args.descriptor.workflow_type:
                raise ValueError("Session belongs to a different agent")
            if description.status != WorkflowExecutionStatus.RUNNING:
                raise ValueError("Session has closed; start a new session")

        if interactive:
            if not existing:
                await temporal.start_workflow(
                    args.descriptor.workflow_type,
                    args=[AgentConfig()]
                    if initial is None
                    else [AgentConfig(), initial],
                    id=session,
                    task_queue=args.descriptor.task_queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                )
            await check_session()
            emit(
                args,
                "progress",
                {
                    "text": f"Session: {session}",
                    "session": session,
                    "idle": input_field not in payload,
                },
            )
            if existing:
                if ui:
                    ui.wait("Loading history…")
                await follow(temporal, client, args, history_field=input_field)
                if ui:
                    ui.flush()
    if interactive:
        emit(
            args,
            "progress",
            {"text": "Type a message and press Enter. /exit or Ctrl+D to leave."},
        )
        if input_field in payload and ui:
            ui.user(payload[input_field])

    result = {"session": session}
    while True:
        if not interactive or input_field in payload:
            async with asyncio.timeout(timeout if interactive else None):
                # Never replace a supplied session with a new conversation.
                if interactive or existing:
                    await check_session()
                    receipt = await client.submit_message(
                        handler, payload, update_id=args.request_id
                    )
                else:
                    receipt = await client.start_and_submit_message(
                        handler,
                        payload,
                        workflow_name=args.descriptor.workflow_type,
                        task_queue=args.descriptor.task_queue,
                        start_config=AgentConfig(),
                        start_data=initial,
                        update_id=args.request_id,
                    )
                accepted = {
                    "session": session,
                    "request_id": args.request_id,
                    "receipt": asdict(receipt),
                }
                if args.detach:
                    return accepted
                if not interactive:
                    emit(args, "progress", {"text": f"Session: {session}", **accepted})
                    if ui and isinstance(
                        prompt := payload.get("prompt", payload.get("text")), str
                    ):
                        ui.user(prompt)
                result = await follow(temporal, client, args, receipt)
            if not interactive:
                return result
            emit(args, "result", result)
            args.request_id = str(uuid.uuid4())
        while True:
            line = await read_line("❯ ")
            if not line or line.strip() == "/exit":
                print(
                    f"\nResume with:\n{resume_command(args, payload, input_field)}",
                    file=sys.stderr,
                    flush=True,
                )
                return result
            if not line.strip():
                continue
            candidate = {**payload, input_field: line.rstrip("\r\n")}
            try:
                validate(doc, doc["handlers"][handler]["input"], candidate)
            except ValueError as error:
                emit(args, "progress", {"text": f"Invalid message: {error}"})
                continue
            payload = candidate
            break
        if ui:
            ui.wait("Working…")


def main(argv: list[str] | None = None) -> int:
    args = None
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        if argv and argv[0] == "help":
            argv = [*argv[1:], "--help"]
        args = parse_args(argv)
        if interactive_output(args):
            try:
                from temporal_agent_harness.agent_cli_ui import TerminalChat
            except ImportError:
                raise ValueError(
                    "Install temporal-agent-harness[cli] for the terminal UI"
                ) from None
            descriptor = getattr(args, "descriptor", None)
            args._ui = TerminalChat(descriptor.label if descriptor else "Agent")
        if hasattr(args, "request_id") and args.request_id is None:
            args.request_id = str(uuid.uuid4())
        result = asyncio.run(
            asyncio.wait_for(
                execute(args),
                None
                if getattr(args, "interactive", False)
                else getattr(args, "timeout", None),
            )
        )
        if not getattr(args, "interactive", False):
            emit(args, "result", result)
        return 1 if "error" in result else 0
    except KeyboardInterrupt:
        error, code = "Stopped following; the agent may still be running.", 130
    except TimeoutError:
        error, code = (
            "Stopped waiting after a timeout; the agent may still be running.",
            1,
        )
    except Exception as caught:
        error, code = str(caught) or type(caught).__name__, 1
    finally:
        if ui := getattr(args, "_ui", None):
            ui.flush()
    if args:
        emit(
            args,
            "error",
            {
                "error": error,
                "session": getattr(args, "session", None),
                "request_id": getattr(args, "request_id", None),
            },
        )
    if not getattr(args, "_ui", None):
        print(f"temporal agent: {error}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
