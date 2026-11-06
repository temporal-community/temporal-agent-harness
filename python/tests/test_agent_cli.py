"""Native schemas, submission safety, and observable agent conversations."""

import argparse
import asyncio
import io
import json
import os
import re
import shlex
import sys
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from rich.console import Console
from temporal_agent_harness import agent_cli
from temporal_agent_harness.agent_cli import interactive_output
from temporal_agent_harness.agent_cli_ui import TerminalChat
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    AgentMessageReply,
    AgentStatus,
    MessageDisposition,
    PendingApproval,
    PendingCallback,
    SubagentInfo,
)
from temporal_agent_harness.harness.agent_protocol.events import (
    AgentEvent,
    CallbackRequested,
    MessageAccepted,
    MessageHandlerEnd,
    MessageHandlerError,
    ReplyDelta,
    ThoughtSummaryDelta,
    ToolEndEvent,
    ToolErrorEvent,
    ToolStartEvent,
    TurnEnded,
)
from temporal_agent_harness.harness.agent_schema import dump_agent_schema
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporal_agent_harness.plugin import AgentHarnessPlugin

from tests.fixture_cli_agent import CLIExampleAgent


def write_registry(directory):
    path = Path(directory) / "agents.toml"
    path.write_text("""[[agents]]
key = "cli-example"
workflow_type = "CLIExampleAgent"
task_queue = "cli-example"
label = "CLI Example"
description = "CLI fixture"
schema = "cli-example.schema.json"
""")
    path.with_name("cli-example.schema.json").write_text(
        dump_agent_schema(CLIExampleAgent)
    )
    return path


class ProjectRegistryTests(unittest.TestCase):
    def test_help_and_session_commands_work_without_a_registry(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            args = agent_cli.parse_args(["status", "session"])
            self.assertEqual(args.command, "status")
            stdout = io.StringIO()
            with redirect_stdout(stdout), self.assertRaises(SystemExit) as exited:
                agent_cli.parse_args(["--help"])
            self.assertEqual(exited.exception.code, 0)
            self.assertIn("--registry", stdout.getvalue())

    def test_top_level_help_works_with_invalid_or_missing_registries(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = write_registry(directory)
            valid = registry.read_text()
            cases = [
                ("malformed", "[[agents]"),
                ("invalid", "agents = []"),
                ("duplicate", valid + valid),
                ("reserved", valid.replace('key = "cli-example"', 'key = "status"')),
                ("missing", None),
            ]
            for name, content in cases:
                with self.subTest(registry=name):
                    selected = Path(directory) / f"{name}.toml"
                    if content is not None:
                        selected.write_text(content)
                    flags = ["--registry", str(selected)]
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as exited:
                            agent_cli.main([*flags, "--help"])
                        self.assertEqual(exited.exception.code, 0)
                        self.assertIn("--registry", stdout.getvalue())
                        self.assertIn("show session status", stdout.getvalue())
                        self.assertEqual(stderr.getvalue(), "")
                        code = agent_cli.main([*flags, "status", "session"])
                    self.assertEqual(code, 1)

    def test_top_level_help_lists_agents_from_a_valid_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = write_registry(directory)
            for flag in ("--help", "-h", "help"):
                with self.subTest(flag=flag):
                    stdout = io.StringIO()
                    with (
                        redirect_stdout(stdout),
                        self.assertRaises(SystemExit) as exited,
                    ):
                        agent_cli.main([flag, "--registry", str(registry)])
                    self.assertEqual(exited.exception.code, 0)
                    self.assertIn("cli-example", stdout.getvalue())

    def test_help_explains_commands_inputs_and_callback_results(self):
        cases = [
            (["--help"], ["show session status", "submit a callback result"]),
            (
                ["cli-example", "echo", "--help"],
                ["validate inputs without connecting", "default: 1"],
            ),
            (["callback", "--help"], ["JSON or @file", "callback failure"]),
        ]
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            write_registry(directory)
            for argv, expected in cases:
                with self.subTest(argv=argv):
                    stdout = io.StringIO()
                    with (
                        redirect_stdout(stdout),
                        self.assertRaises(SystemExit) as exited,
                    ):
                        agent_cli.main(argv)
                    self.assertEqual(exited.exception.code, 0)
                    help_text = " ".join(stdout.getvalue().split())
                    for description in expected:
                        self.assertIn(description, help_text)

    def test_project_registry_is_used_by_default(self):
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            write_registry(directory)
            args = agent_cli.parse_args(["cli-example", "echo", "--text", "hello"])
        self.assertEqual(args.descriptor.key, "cli-example")

    def test_explicit_missing_registry_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                agent_cli.parse_args(
                    [
                        "--registry",
                        str(Path(directory) / "missing.toml"),
                        "status",
                        "session",
                    ]
                )

    def test_local_registry_precedes_saved_registry_and_explicit_selection_wins(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            chdir(directory),
            patch.dict(os.environ, XDG_CONFIG_HOME=str(Path(directory) / "config")),
        ):
            saved = Path(directory) / "config/temporal-agent"
            saved.mkdir(parents=True)
            saved_registry = write_registry(saved)
            self.assertEqual(
                agent_cli.parse_args(["status", "session"]).registry,
                saved_registry.resolve(),
            )
            local = write_registry(directory)
            self.assertEqual(
                agent_cli.parse_args(["status", "session"]).registry, local.resolve()
            )
            args = agent_cli.parse_args(
                ["--registry", str(saved_registry), "status", "session"]
            )
            self.assertEqual(args.registry, saved_registry.resolve())


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.registry = write_registry(self.directory.name)

    def invoke(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = agent_cli.main(
                ["--registry", str(self.registry), "--output", "json", *argv]
            )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_flags_use_native_types_and_leave_omitted_defaults_to_the_agent(self):
        tags = Path(self.directory.name) / "tags.json"
        tags.write_text('["one", "two"]')
        code, result, _ = self.invoke(
            "cli-example",
            "echo",
            "--text",
            "hello",
            "--count",
            "2",
            "--no-enabled",
            "--tags",
            "@" + str(tags),
            "--metadata",
            '{"key":"value"}',
            "--arg-profile",
            "inside-input",
            "--init-prefix",
            "prefix:",
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["handler"], "echo")
        self.assertEqual(
            result["input"],
            {
                "text": "hello",
                "count": 2,
                "enabled": False,
                "tags": ["one", "two"],
                "metadata": {"key": "value"},
                "profile": "inside-input",
            },
        )
        self.assertEqual(result["init_data"], {"prefix": "prefix:"})

    def test_complete_json_handles_nullable_and_nested_inputs(self):
        code, result, _ = self.invoke(
            "cli-example",
            "echo",
            "--input",
            '{"text":"hi","profile":null}',
            "--init",
            '{"prefix":""}',
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["input"], {"text": "hi", "profile": None})

    def test_missing_cli_extra_reports_how_to_install_it(self):
        with patch.dict(sys.modules, {"jsonschema": None}):
            code, result, _ = self.invoke(
                "cli-example",
                "echo",
                "--text",
                "hello",
                "--init-prefix",
                "",
                "--dry-run",
            )
        self.assertEqual(code, 1)
        self.assertIn("temporal-agent-harness[cli]", result["error"])

    def test_invalid_inputs_fail_before_connecting(self):
        cases = [
            ([], "text"),
            (["--text", "hi"], "prefix"),
            (["--text", "hi", "--init-prefix", "", "--count", "0"], "minimum"),
            (["--text", "hi", "--init-prefix", "", "--mode", "unknown"], "not one of"),
            (["--text", "hi", "--input", '{"text":"hi"}'], "either --input"),
            (
                ["--text", "hi", "--session", "existing", "--init-prefix", ""],
                "new session",
            ),
        ]
        with patch.object(agent_cli.Client, "connect") as connect:
            for flags, message in cases:
                with self.subTest(flags=flags):
                    code, result, _ = self.invoke("cli-example", "echo", *flags)
                    self.assertEqual(code, 1)
                    self.assertIn(message, result["error"])
            connect.assert_not_called()

    def test_explicit_schema_works_without_installed_agent_modules(self):
        schema = self.registry.with_name("cli-example.schema.json")
        schema.rename(self.registry.with_name("custom.schema.json"))
        self.registry.write_text(
            self.registry.read_text().replace(
                "cli-example.schema.json", "custom.schema.json"
            )
            + 'agent = "uninstalled.agent:Unknown"\n'
        )
        code, result, _ = self.invoke(
            "cli-example", "echo", "--text", "hi", "--session", "existing", "--dry-run"
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["handler"], "echo")

    def test_agent_invocation_requires_a_schema_reference(self):
        self.registry.write_text(
            self.registry.read_text().replace(
                'schema = "cli-example.schema.json"\n', ""
            )
        )
        with self.assertRaisesRegex(ValueError, "set schema"):
            agent_cli.parse_args(
                ["--registry", str(self.registry), "cli-example", "--help"]
            )

    def test_schema_identity_must_match_the_registry(self):
        schema = self.registry.with_name("cli-example.schema.json")
        document = json.loads(schema.read_text())
        document["agent"] = "AnotherAgent"
        schema.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "identity disagree"):
            agent_cli.parse_args(
                ["--registry", str(self.registry), "cli-example", "--help"]
            )

    def test_duplicate_agent_keys_are_rejected(self):
        self.registry.write_text(self.registry.read_text() * 2)
        with self.assertRaisesRegex(ValueError, "Duplicate agent key"):
            agent_cli.parse_args(
                ["--registry", str(self.registry), "status", "session"]
            )

    def test_only_the_selected_agents_schema_is_needed(self):
        self.registry.write_text(
            self.registry.read_text()
            + '\n[[agents]]\nkey = "other"\nworkflow_type = "OtherAgent"\n'
            'task_queue = "other"\nlabel = "Other"\ndescription = "Other agent"\n'
            'schema = "missing.schema.json"\n'
        )
        args = agent_cli.parse_args(
            ["--registry", str(self.registry), "cli-example", "echo", "--text", "hi"]
        )
        self.assertEqual(args.descriptor.key, "cli-example")

    def test_session_commands_do_not_need_schema_documents(self):
        self.registry.with_name("cli-example.schema.json").unlink()
        args = agent_cli.parse_args(
            ["--registry", str(self.registry), "status", "session"]
        )
        self.assertEqual(args.command, "status")


def envelope(event, *, agent="root", message="mine", turn="turn-2"):
    return AgentEvent(
        agent_id=agent,
        message_id=message,
        turn_id=turn,
        turn_number=2,
        timestamp=0,
        event=event,
    )


def stream_client(events, status=None):
    client = Mock(spec=AgentClient)
    client.workflow_id = "session"
    client.get_status = AsyncMock(return_value=status or AgentStatus(agent_id="root"))
    client.get_pending_approvals = AsyncMock(return_value=[])
    client.get_pending_callbacks = AsyncMock(return_value=[])
    client.approve_tool = AsyncMock()
    client.provide_callback_result = AsyncMock()

    async def attach(*, on_item, from_offset):
        async def stream():
            for offset, event in enumerate(events, from_offset + 1):
                yield on_item(event, offset)

        return stream()

    client.attach = AsyncMock(side_effect=attach)
    return client


class TextOutputTests(unittest.IsolatedAsyncioTestCase):
    async def render(self, events, output="text", *, terminal=False):
        args = argparse.Namespace(output=output)
        receipt = AgentMessageReply(2, "turn-2", "mine", MessageDisposition.OPENED, 0)
        stdout, stderr = io.StringIO(), io.StringIO()
        if terminal:
            args._ui = TerminalChat(
                "Chat", console=Console(file=stderr, width=60, height=12)
            )
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = await agent_cli.follow(None, stream_client(events), args, receipt)
            agent_cli.emit(args, "result", result)
        return stdout.getvalue(), stderr.getvalue()

    async def test_streamed_reply_is_shown_once_without_a_result_envelope(self):
        stdout, stderr = await self.render(
            [
                envelope(ReplyDelta(text="Hello, ")),
                envelope(ReplyDelta(text="world!")),
                envelope(MessageHandlerEnd(output={"text": "Hello, world!"})),
                envelope(TurnEnded(), message=None),
            ]
        )
        self.assertEqual(stderr, "Hello, world!\n")
        self.assertEqual(stdout, "")

    async def test_reply_chunks_are_visible_before_the_final_response(self):
        def events():
            yield envelope(ReplyDelta(text="Hello, "))
            self.assertEqual(sys.stderr.getvalue(), "Hello, ")
            yield envelope(ReplyDelta(text="world!"))
            self.assertEqual(sys.stderr.getvalue(), "Hello, world!")
            yield envelope(MessageHandlerEnd(output={"text": "Hello, world!"}))
            yield envelope(TurnEnded(), message=None)

        await self.render(events())

    async def test_final_response_is_preserved_when_it_was_not_streamed_in_full(self):
        for chunks, final, expected in [
            ([], {"text": "Final answer"}, "Final answer\n"),
            (["Draft"], {"text": "Final answer"}, "Final answer\n"),
            (["Summary"], {"text": "Summary", "findings": []}, None),
        ]:
            with self.subTest(chunks=chunks, final=final):
                stdout, _ = await self.render(
                    [
                        *(envelope(ReplyDelta(text=text)) for text in chunks),
                        envelope(MessageHandlerEnd(output=final)),
                        envelope(TurnEnded(), message=None),
                    ]
                )
                if expected is None:
                    self.assertEqual(json.loads(stdout), final)
                else:
                    self.assertEqual(stdout, expected)

    async def test_other_agents_and_messages_do_not_hide_the_final_response(self):
        stdout, _ = await self.render(
            [
                envelope(ReplyDelta(text="Answer"), agent="child"),
                envelope(ReplyDelta(text="Answer"), message="other"),
                envelope(MessageHandlerEnd(output={"text": "Answer"})),
                envelope(TurnEnded(), message=None),
            ]
        )
        self.assertEqual(stdout, "Answer\n")

    async def test_json_output_keeps_the_complete_result(self):
        stdout, _ = await self.render(
            [
                envelope(ReplyDelta(text="Answer")),
                envelope(MessageHandlerEnd(output={"text": "Answer"})),
                envelope(TurnEnded(), message=None),
            ],
            output="json",
        )
        result = json.loads(stdout)
        self.assertEqual(result["result"], {"text": "Answer"})
        self.assertEqual(result["session"], "session")
        self.assertEqual(result["receipt"]["message_id"], "mine")

    async def test_terminal_transcript_separates_reasoning_tools_and_markdown(self):
        stdout, stderr = await self.render(
            [
                envelope(
                    ThoughtSummaryDelta(
                        delta={
                            "type": "response.reasoning_summary_text.delta",
                            "delta": "Checking the source.",
                        }
                    )
                ),
                envelope(ToolStartEvent(tool_id="read", tool_name="read_file")),
                envelope(ToolEndEvent(tool_id="read", tool_name="read_file")),
                envelope(
                    ToolErrorEvent(
                        tool_id="search", tool_name="search", message="Unavailable"
                    )
                ),
                envelope(ReplyDelta(text="The **answer** is ")),
                envelope(ReplyDelta(text="ready.")),
                envelope(
                    MessageHandlerEnd(output={"text": "The **answer** is ready."})
                ),
                envelope(TurnEnded(), message=None),
            ],
            terminal=True,
        )
        self.assertEqual(stdout, "")
        self.assertIn("● Thinking", stderr)
        self.assertIn("Checking the source.", stderr)
        self.assertEqual(stderr.count("read_file"), 1)
        self.assertIn("✓ read_file", stderr)
        self.assertIn("✗ search", stderr)
        self.assertIn("● Chat", stderr)
        self.assertNotIn("root", stderr)
        self.assertEqual(stderr.count("The answer is ready."), 1)
        self.assertIn("✓ Done", stderr)


class Terminal(io.StringIO):
    def isatty(self):
        return True


class TerminalOutputTests(unittest.TestCase):
    @patch.dict(os.environ, TERM="xterm-256color")
    def test_code_formatted_links_are_clickable_without_background_color(self):
        url = "https://github.com/temporalio/temporal-proxy"
        reply = f"The [`temporalio/temporal-proxy`]({url}) project."
        for streamed in (False, True):
            output = Terminal()
            with self.subTest(streamed=streamed), redirect_stderr(output):
                ui = TerminalChat("Chat")
                ui.console.no_color = False
                try:
                    if streamed:
                        ui.progress({"event": "reply_delta", "text": reply})
                    ui.emit(
                        "result",
                        {"result": {"text": reply}},
                        reply if streamed else None,
                    )
                finally:
                    ui.flush()
            rendered = output.getvalue()
            self.assertRegex(rendered, r"\x1b\]8;[^;]*;" + re.escape(url))
            self.assertIn("temporalio/temporal-proxy", rendered)
            for codes in re.findall(r"\x1b\[([0-9;]*)m", rendered):
                self.assertNotRegex(codes, r"(?:^|;)(?:4[0-7]|10[0-7]|48)(?:;|$)")

    def test_parallel_tool_calls_keep_running_status_out_of_scrollback(self):
        output = io.StringIO()
        ui = TerminalChat("Chat", console=Console(file=output, width=60, height=12))
        self.addCleanup(ui.flush)
        for owner in ("root", "child"):
            ui.progress(
                {
                    "event": "tool_start",
                    "text": "tool_start: run_travel_code",
                    "agent": owner,
                    "tool_id": "call",
                }
            )
        with ui.console.capture() as running:
            ui.console.print(ui.viewport())
        self.assertEqual(running.get().count("run_travel_code · running"), 2)

        ui.progress(
            {
                "event": "tool_end",
                "text": "tool_end: run_travel_code",
                "agent": "child",
                "tool_id": "call",
            }
        )
        with ui.console.capture() as running:
            ui.console.print(ui.viewport())
        self.assertEqual(running.get().count("run_travel_code · running"), 1)

        ui.progress(
            {
                "event": "tool_error",
                "text": "tool_error: run_travel_code",
                "agent": "root",
                "tool_id": "call",
            }
        )
        ui.flush()
        self.assertEqual(output.getvalue().count("✓ run_travel_code"), 1)
        self.assertEqual(output.getvalue().count("✗ run_travel_code"), 1)
        self.assertNotIn("running", output.getvalue())

    def test_animation_is_only_enabled_for_interactive_text_conversations(self):
        terminal = Mock(isatty=lambda: True)
        redirected = io.StringIO()
        cases = [
            ({}, terminal, terminal, "xterm", True),
            ({"output": "json"}, terminal, terminal, "xterm", False),
            ({"detach": True}, terminal, terminal, "xterm", False),
            ({"dry_run": True}, terminal, terminal, "xterm", False),
            ({"command": "status"}, terminal, terminal, "xterm", False),
            ({}, redirected, terminal, "xterm", False),
            ({}, terminal, redirected, "xterm", False),
            ({}, terminal, terminal, "dumb", False),
        ]
        for flags, stdout, stderr, term, expected in cases:
            args = argparse.Namespace(**{"command": "follow", **flags})
            with (
                self.subTest(flags=flags, term=term),
                patch.object(sys, "stdout", stdout),
                patch.object(sys, "stderr", stderr),
                patch.dict(os.environ, TERM=term),
            ):
                self.assertEqual(interactive_output(args), expected)

    @patch.dict(os.environ, TERM="xterm")
    def test_long_reply_shows_latest_text_and_preserves_full_scrollback(self):
        output = io.StringIO()
        ui = TerminalChat(
            "Chat",
            console=Console(
                file=output,
                force_terminal=True,
                width=40,
                height=10,
            ),
        )
        self.addCleanup(ui.flush)
        text = "\n\n".join(f"Paragraph {i}" for i in range(30))
        ui.progress({"event": "reply_delta", "agent": "root", "text": text})
        self.assertNotIn("Paragraph 0", output.getvalue())
        self.assertIn("Paragraph 29", output.getvalue())
        ui.emit("result", {"result": {"text": text}}, text)
        self.assertIn("Paragraph 0", output.getvalue())
        self.assertIn("Paragraph 15", output.getvalue())
        self.assertIn("\x1b[?25h", output.getvalue())

    def test_thought_text_reads_every_provider_shape(self):
        for delta, expected in (
            ({"content": {"type": "text", "text": "gemini"}}, "gemini"),
            ({"delta": "openai", "item_id": "x"}, "openai"),
            ({"content": "pydantic", "part_kind": "thinking"}, "pydantic"),
            ({"content_delta": "more", "signature_delta": "SECRET"}, "more"),
            ({"signature_delta": "SECRET"}, None),
            ({"content": {"type": "image"}}, None),
        ):
            self.assertEqual(agent_cli.thought_text(delta), expected)

    def test_subagent_replies_are_labelled_as_the_subagent(self):
        output = Terminal()
        ui = TerminalChat("Root", console=Console(file=output, width=40))
        self.addCleanup(ui.flush)
        ui.progress({"event": "reply_delta", "agent": "root", "text": "from root"})
        ui.progress(
            {
                "event": "reply_delta",
                "agent": "child",
                "subagent": True,
                "text": "from child",
            }
        )
        ui.flush()
        self.assertIn("● Root", output.getvalue())
        self.assertIn("● Subagent child", output.getvalue())

    def test_interrupt_restores_terminal_and_keeps_partial_reply(self):
        def events():
            yield envelope(ReplyDelta(text="Partial answer"))
            raise KeyboardInterrupt

        stdout, stderr = Terminal(), Terminal()
        with tempfile.TemporaryDirectory() as directory:
            registry = write_registry(directory)
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                patch.dict(os.environ, TERM="xterm"),
                patch.object(agent_cli.Client, "connect", new=AsyncMock()),
                patch.object(
                    agent_cli, "AgentClient", return_value=stream_client(events())
                ),
            ):
                code = agent_cli.main(
                    ["--registry", str(registry), "follow", "session"]
                )
        self.assertEqual(code, 130)
        self.assertEqual(stdout.getvalue(), "")
        transcript = stderr.getvalue()
        self.assertIn("Partial answer", transcript)
        self.assertIn("Stopped following", transcript)
        self.assertIn("\x1b[?25h", transcript)

    def test_interactive_conversation_displays_each_reply_once(self):
        client = stream_client(
            [
                envelope(MessageHandlerEnd(output={"text": "**Ready** to go."})),
                envelope(TurnEnded(), message=None),
            ]
        )
        client.submit_message = AsyncMock(
            return_value=AgentMessageReply(
                2,
                "turn-2",
                "mine",
                MessageDisposition.OPENED,
                0,
            )
        )
        temporal = Mock(spec=agent_cli.Client)
        temporal.start_workflow = AsyncMock()
        temporal.get_workflow_handle.return_value.describe = AsyncMock(
            return_value=Mock(
                workflow_type="CLIExampleAgent", status=WorkflowExecutionStatus.RUNNING
            )
        )
        stdout, stderr = Terminal(), Terminal()
        with tempfile.TemporaryDirectory() as directory:
            registry = write_registry(directory)
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                patch.dict(os.environ, TERM="xterm", NO_COLOR="1"),
                patch.object(sys.stdin, "isatty", return_value=True),
                patch.object(
                    agent_cli, "read_line", new=AsyncMock(return_value="/exit\n")
                ),
                patch.object(
                    agent_cli.Client, "connect", new=AsyncMock(return_value=temporal)
                ),
                patch.object(agent_cli, "AgentClient", return_value=client),
            ):
                code = agent_cli.main(
                    [
                        "--registry",
                        str(registry),
                        "cli-example",
                        "echo",
                        "--text",
                        "Hello there",
                        "--init-prefix",
                        "",
                        "--interactive",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        transcript = stderr.getvalue()
        self.assertIn("CLI Example", transcript)
        self.assertIn("❯ You", transcript)
        self.assertIn("Hello there", transcript)
        self.assertEqual(transcript.count("to go."), 1)
        self.assertNotIn("**Ready**", transcript)
        self.assertIn("✓ Done", transcript)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.args = argparse.Namespace(output="json")
        self.receipt = AgentMessageReply(
            2, "turn-2", "mine", MessageDisposition.JOINED, 12
        )

    async def test_joined_turn_matches_message_and_root_and_hides_private_thoughts(
        self,
    ):
        client = stream_client(
            [
                envelope(
                    ThoughtSummaryDelta(
                        delta={"text": "PRIVATE", "signature": "SECRET"}
                    )
                ),
                envelope(
                    ThoughtSummaryDelta(
                        delta={
                            "type": "response.reasoning_summary_text.delta",
                            "delta": "Public summary",
                        }
                    )
                ),
                envelope(ReplyDelta(text="Progress")),
                envelope(MessageHandlerEnd(output={"text": "child"}), agent="child"),
                envelope(MessageHandlerEnd(output={"text": "other"}), message="other"),
                envelope(MessageHandlerEnd(output={"text": "mine"})),
                envelope(TurnEnded(), message=None),
            ]
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = await agent_cli.follow(None, client, self.args, self.receipt)
        self.assertEqual(result["result"], {"text": "mine"})
        self.assertIn("Public summary", stderr.getvalue())
        self.assertIn("Progress", stderr.getvalue())
        self.assertNotIn("PRIVATE", stderr.getvalue())
        self.assertNotIn("SECRET", stderr.getvalue())

    async def test_handler_error_is_not_confused_with_session_completion(self):
        client = stream_client(
            [
                envelope(MessageHandlerError(message="failed")),
                envelope(TurnEnded(), message=None),
            ]
        )
        result = await agent_cli.follow(None, client, self.args, self.receipt)
        self.assertEqual(result["error"], "failed")

    async def test_stream_without_matching_response_is_a_failure(self):
        client = stream_client([envelope(TurnEnded(), message=None)])
        with self.assertRaisesRegex(ValueError, "without a response"):
            await agent_cli.follow(None, client, self.args, self.receipt)

    async def test_full_replay_returns_the_latest_response_after_an_earlier_failure(
        self,
    ):
        client = stream_client(
            [
                envelope(MessageHandlerError(message="earlier failure")),
                envelope(
                    MessageHandlerEnd(output={"text": "recovered"}), message="later"
                ),
                envelope(TurnEnded(), message=None),
            ]
        )
        result = await agent_cli.follow(None, client, self.args)
        self.assertEqual(result["result"], {"text": "recovered"})
        self.assertEqual(client.attach.call_args.kwargs["from_offset"], 0)
        self.assertNotIn("error", result)

    async def test_resumed_child_approval_requires_an_explicit_human_decision(self):
        child = stream_client(
            [],
            AgentStatus(
                agent_id="child",
                pending_approvals=[
                    PendingApproval("tool", "gated_echo", {"text": "hi"}, 2, "short")
                ],
            ),
        )
        root = stream_client(
            [],
            AgentStatus(
                agent_id="root",
                subagents=[SubagentInfo("child", "child-agent", "child-session")],
            ),
        )
        with (
            patch.object(agent_cli, "AgentClient", return_value=child),
            patch.object(agent_cli.sys.stdin, "isatty", return_value=False),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaisesRegex(ValueError, "approve session tool"):
                await agent_cli.follow(None, root, self.args)
        child.approve_tool.assert_not_called()

    async def test_gate_recovery_commands_preserve_connection_settings(self):
        statuses = [
            AgentStatus(
                agent_id="root",
                pending_approvals=[
                    PendingApproval("tool id", "gated_echo", {}, 2, "short")
                ],
            ),
            AgentStatus(
                agent_id="root",
                pending_callbacks=[PendingCallback("tool id", "local_echo", {}, {}, 2)],
            ),
        ]
        args = argparse.Namespace(
            output="json",
            profile="Cloud team",
            address="custom:7233",
            namespace="review-space",
        )
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            write_registry(directory)
            Path("result.json").write_text("{}")
            for status in statuses:
                with (
                    self.subTest(status=status),
                    patch.object(sys.stdin, "isatty", return_value=False),
                ):
                    client = stream_client([], status)
                    client.workflow_id = "session with spaces"
                    with self.assertRaises(ValueError) as failed:
                        await agent_cli.follow(None, client, args)
                    command = str(failed.exception).split(": ", 1)[1]
                    tokens = shlex.split(command.removesuffix(" (or deny)"))
                    self.assertEqual(tokens[:2], ["temporal", "agent"])
                    restored = agent_cli.parse_args(tokens[2:])
                    self.assertEqual(restored.session, "session with spaces")
                    self.assertEqual(restored.tool_id, "tool id")
                    self.assertEqual(restored.profile, "Cloud team")
                    self.assertEqual(restored.address, "custom:7233")
                    self.assertEqual(restored.namespace, "review-space")

    async def test_interactive_approval_defaults_to_denial(self):
        client = stream_client([])
        event = PendingApproval("tool", "gated_echo", {}, 2, "short")
        descriptor, writer = os.pipe()
        self.addCleanup(os.close, writer)
        reader = self.enterContext(os.fdopen(descriptor))
        os.write(writer, b"\n")
        with (
            patch.object(agent_cli.sys, "stdin", reader),
            patch.object(reader, "isatty", return_value=True),
            redirect_stderr(io.StringIO()),
        ):
            await agent_cli.request_approval(client, event, self.args)
        client.approve_tool.assert_awaited_once_with("tool", approved=False)

    async def test_script_approval_shows_code_before_prompting(self):
        script = 'import asyncio\n\nasync def main():\n    flights = await search_flights({"origin": "JFK", "destination": "BOS"})\n    return flights\n\nasyncio.run(main())'
        event = PendingApproval(
            "tool",
            "run_travel_code",
            {"script": script, "trip": "Boston"},
            2,
            "Search flights",
        )
        for rich in (False, True):
            with self.subTest(rich=rich):
                client = stream_client([])
                output = io.StringIO()
                args = argparse.Namespace(output="text")
                if rich:
                    args._ui = TerminalChat(
                        "Chat", console=Console(file=output, width=60, height=12)
                    )

                async def answer(prompt):
                    rendered = output.getvalue()
                    self.assertIn("run_travel_code", rendered)
                    self.assertIn("import asyncio", rendered)
                    self.assertIn("async def main():", rendered)
                    self.assertIn("return flights", rendered)
                    self.assertIn("asyncio.run(main())", rendered)
                    self.assertIn("Boston", rendered)
                    self.assertNotIn(r"\n", rendered)
                    self.assertNotIn(r"\"", rendered)
                    if rich:
                        self.assertIn("1 import asyncio", rendered)
                        self.assertIn("Approval required:", rendered)
                    return "y\n"

                with (
                    patch.object(agent_cli.sys.stdin, "isatty", return_value=True),
                    patch.object(agent_cli, "read_line", side_effect=answer),
                    redirect_stderr(output),
                ):
                    await agent_cli.request_approval(client, event, args)
                client.approve_tool.assert_awaited_once_with("tool", approved=True)

    async def test_timeout_leaves_pending_approval_undecided(self):
        client = stream_client([])
        event = PendingApproval("tool", "gated_echo", {}, 2, "short")
        descriptor, writer = os.pipe()
        self.addCleanup(os.close, writer)
        reader = self.enterContext(os.fdopen(descriptor))
        with (
            patch.object(agent_cli.sys, "stdin", reader),
            patch.object(reader, "isatty", return_value=True),
            redirect_stderr(io.StringIO()),
            self.assertRaises(TimeoutError),
        ):
            await asyncio.wait_for(
                agent_cli.request_approval(client, event, self.args), 0.01
            )
        client.approve_tool.assert_not_called()

    async def test_pending_and_live_callbacks_require_a_manual_result(self):
        pending = PendingCallback("callback", "local_echo", {"text": "hi"}, {}, 2)
        event = CallbackRequested(
            tool_id="callback", tool_name="local_echo", tool_input={"text": "hi"}
        )
        for already_pending in (True, False):
            with self.subTest(already_pending=already_pending):
                client = stream_client(
                    [] if already_pending else [envelope(event)],
                    AgentStatus(
                        agent_id="root",
                        pending_callbacks=[pending] if already_pending else [],
                    ),
                )
                client.get_pending_callbacks.return_value = [pending]
                with self.assertRaisesRegex(
                    ValueError,
                    "temporal agent callback session callback --result @result.json",
                ):
                    await agent_cli.follow(None, client, self.args)
                client.provide_callback_result.assert_not_called()

    async def test_interactive_questions_are_answered_once_when_live_or_resumed(self):
        questions = [
            PendingCallback(
                "departure",
                "ask_user",
                {"question": "Where from?"},
                {"type": "string"},
                2,
            ),
            PendingCallback(
                "weekend",
                "ask_user",
                {
                    "question": "Which weekend?",
                    "options": ["This weekend", "Next weekend"],
                },
                {"type": "string"},
                2,
            ),
        ]
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                pending = list(questions)
                client = stream_client(
                    [
                        envelope(
                            CallbackRequested(
                                tool_id=q.tool_id,
                                tool_name=q.tool_name,
                                tool_input=q.tool_input,
                                output_schema=q.output_schema,
                            )
                        )
                        for q in questions
                    ],
                    AgentStatus(
                        agent_id="root",
                        pending_callbacks=list(questions) if resumed else [],
                    ),
                )
                client.get_pending_callbacks.side_effect = lambda: list(pending)

                async def resolve(tool_id, *, result):
                    pending[:] = [q for q in pending if q.tool_id != tool_id]

                client.provide_callback_result.side_effect = resolve
                args = argparse.Namespace(interactive=True, output="text")
                descriptor, writer = os.pipe()
                reader = self.enterContext(os.fdopen(descriptor))
                os.write(writer, b"\nNew York\nNext weekend\n")
                os.close(writer)
                transcript = io.StringIO()
                with (
                    patch.object(agent_cli.sys, "stdin", reader),
                    patch.object(reader, "isatty", return_value=True),
                    redirect_stderr(transcript),
                ):
                    await agent_cli.follow(None, client, args)
                self.assertEqual(client.provide_callback_result.await_count, 2)
                self.assertEqual(
                    client.provide_callback_result.await_args_list[0].args,
                    ("departure",),
                )
                self.assertEqual(
                    client.provide_callback_result.await_args_list[0].kwargs,
                    {"result": "New York"},
                )
                self.assertEqual(
                    client.provide_callback_result.await_args_list[1].kwargs,
                    {"result": "Next weekend"},
                )
                self.assertEqual(transcript.getvalue().count("Where from?"), 1)
                self.assertEqual(transcript.getvalue().count("Which weekend?"), 1)
                self.assertIn("• This weekend", transcript.getvalue())

    async def test_question_without_a_human_answer_remains_pending(self):
        event = PendingCallback(
            "question", "ask_user", {"question": "Where from?"}, {"type": "string"}, 2
        )
        args = argparse.Namespace(interactive=True, output="text")
        for outcome in ("", asyncio.CancelledError()):
            with self.subTest(outcome=outcome):
                client = stream_client([])
                read = (
                    AsyncMock(return_value=outcome)
                    if isinstance(outcome, str)
                    else AsyncMock(side_effect=outcome)
                )
                with (
                    patch.object(agent_cli.sys.stdin, "isatty", return_value=True),
                    patch.object(agent_cli, "read_line", read),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(
                        ValueError
                        if isinstance(outcome, str)
                        else asyncio.CancelledError
                    ),
                ):
                    await agent_cli.request_callback(client, event, args)
                client.provide_callback_result.assert_not_called()

    async def test_callback_prompt_requires_terminal_text_and_a_question_contract(
        self,
    ):
        question = {"question": "Where?"}
        string = {"type": "string"}
        cases = [
            ("text", False, "ask_user", question, string),
            ("json", True, "ask_user", question, string),
            ("text", True, "local_echo", question, string),
            ("text", True, "ask_user", {"question": 42}, string),
            ("text", True, "ask_user", {**question, "options": "invalid"}, string),
            ("text", True, "ask_user", question, {"type": "object"}),
        ]
        for output, terminal, name, inputs, schema in cases:
            with self.subTest(
                output=output,
                terminal=terminal,
                name=name,
                inputs=inputs,
                schema=schema,
            ):
                client = stream_client([])
                event = PendingCallback("callback", name, inputs, schema, 2)
                args = argparse.Namespace(output=output)
                with (
                    patch.object(agent_cli.sys.stdin, "isatty", return_value=terminal),
                    patch.object(
                        agent_cli, "read_line", new_callable=AsyncMock
                    ) as read,
                    self.assertRaisesRegex(ValueError, "Callback required"),
                ):
                    await agent_cli.request_callback(client, event, args)
                read.assert_not_called()
                client.provide_callback_result.assert_not_called()


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.registry = write_registry(self.directory.name)
        self.temporal = Mock()
        self.temporal.start_workflow = AsyncMock()
        self.temporal.get_workflow_handle.return_value.describe = AsyncMock(
            return_value=Mock(
                workflow_type="CLIExampleAgent", status=WorkflowExecutionStatus.RUNNING
            )
        )
        self.agent = Mock(spec=AgentClient)
        self.agent.workflow_id = "session"
        receipt = AgentMessageReply(1, "turn", "message", MessageDisposition.OPENED)
        self.agent.start_and_submit_message = AsyncMock(return_value=receipt)
        self.agent.submit_message = AsyncMock(return_value=receipt)
        self.agent.get_status = AsyncMock(return_value=AgentStatus(agent_id="root"))
        self.agent.attach = stream_client([]).attach
        self.connect = self.enterContext(
            patch.object(
                agent_cli.Client, "connect", AsyncMock(return_value=self.temporal)
            )
        )
        self.enterContext(
            patch.object(agent_cli, "AgentClient", return_value=self.agent)
        )
        self.config = self.enterContext(
            patch.object(
                agent_cli.ClientConfig,
                "load_client_connect_config",
                return_value={
                    "target_host": "profile:7233",
                    "namespace": "profile-ns",
                    "api_key": "synthetic",
                },
            )
        )

    def args(self, *flags):
        args = agent_cli.parse_args(
            [
                "--registry",
                str(self.registry),
                "cli-example",
                "echo",
                "--text",
                "hi",
                *flags,
            ]
        )
        args.request_id = "stable-request"
        return args

    async def test_new_session_starts_and_submits_atomically_with_native_init(self):
        first = await agent_cli.execute(
            self.args(
                "--detach",
                "--init-prefix",
                "prefix:",
                "--address",
                "override:7233",
                "--profile",
                "work",
            )
        )
        second = await agent_cli.execute(
            self.args("--detach", "--init-prefix", "prefix:")
        )
        self.assertEqual(first["session"], second["session"])
        call = self.agent.start_and_submit_message.call_args
        self.assertEqual(call.args, ("echo", {"text": "hi"}))
        self.assertEqual(call.kwargs["start_data"], {"prefix": "prefix:"})
        self.assertEqual(call.kwargs["update_id"], "stable-request")
        self.assertEqual(
            self.connect.await_args_list[0].kwargs["target_host"], "override:7233"
        )
        self.assertEqual(self.connect.await_args_list[0].kwargs["api_key"], "synthetic")
        self.assertEqual(self.config.call_args_list[0].kwargs, {"profile": "work"})

    async def test_detached_submission_does_not_print_interactive_prompts(self):
        with redirect_stderr(io.StringIO()) as output:
            await agent_cli.execute(self.args("--detach", "--init-prefix", ""))
        self.assertEqual(output.getvalue(), "")

    async def test_manual_callback_submits_the_supplied_result_or_error(self):
        for flags, result, error in [
            (["--result", '{"text":"done"}'], {"text": "done"}, None),
            (["--error", "failed"], None, "failed"),
        ]:
            with self.subTest(flags=flags):
                args = agent_cli.parse_args(
                    [
                        "--registry",
                        str(self.registry),
                        "callback",
                        "existing",
                        "tool",
                        *flags,
                    ]
                )
                response = await agent_cli.execute(args)
                self.assertTrue(response["accepted"])
                self.agent.provide_callback_result.assert_awaited_with(
                    "tool", result=result, error=error
                )

    async def test_existing_session_must_be_open_and_belong_to_the_agent(self):
        describe = AsyncMock(
            return_value=Mock(
                workflow_type="WrongAgent", status=WorkflowExecutionStatus.RUNNING
            )
        )
        self.temporal.get_workflow_handle.return_value.describe = describe
        for workflow_type, status, message in [
            ("WrongAgent", WorkflowExecutionStatus.RUNNING, "different agent"),
            ("CLIExampleAgent", WorkflowExecutionStatus.COMPLETED, "has closed"),
        ]:
            describe.return_value = Mock(workflow_type=workflow_type, status=status)
            with self.assertRaisesRegex(ValueError, message):
                await agent_cli.execute(self.args("--detach", "--session", "existing"))
        self.agent.submit_message.assert_not_called()
        self.agent.start_and_submit_message.assert_not_called()
        describe.return_value = Mock(
            workflow_type="CLIExampleAgent", status=WorkflowExecutionStatus.RUNNING
        )
        await agent_cli.execute(self.args("--detach", "--session", "existing"))
        self.agent.submit_message.assert_awaited_once_with(
            "echo", {"text": "hi"}, update_id="stable-request"
        )

    async def test_interactive_followups_reuse_session_and_validate_each_message(self):
        args = self.args("--interactive", "--init-prefix", "prefix:", "--count", "2")
        args.document["$defs"]["Message"]["properties"]["text"]["maxLength"] = 3
        self.agent.submit_message.side_effect = [
            self.agent.start_and_submit_message.return_value,
            AgentMessageReply(2, "turn-2", "second", MessageDisposition.OPENED, 10),
        ]

        async def attach(*, on_item, from_offset):
            message, turn, text = (
                ("message", "turn", "First reply")
                if from_offset == 0
                else ("second", "turn-2", "Second reply")
            )

            async def stream():
                for offset, event in enumerate(
                    [
                        ReplyDelta(text=text),
                        MessageHandlerEnd(output={"text": text}),
                        TurnEnded(),
                    ],
                    from_offset + 1,
                ):
                    yield on_item(envelope(event, message=message, turn=turn), offset)

            return stream()

        self.agent.attach = AsyncMock(side_effect=attach)
        descriptor, writer = os.pipe()
        reader = self.enterContext(os.fdopen(descriptor, encoding="utf-8"))
        os.write(writer, "\nmessage too long\nhé\n/exit\n".encode())
        os.close(writer)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "stdin", reader),
            patch.object(reader, "isatty", return_value=True),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = await agent_cli.execute(args)

        self.assertEqual(result["result"], {"text": "Second reply"})
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue().count("First reply"), 1)
        self.assertEqual(stderr.getvalue().count("Second reply"), 1)
        self.assertIn("too long", stderr.getvalue())
        self.connect.assert_awaited_once()
        self.temporal.start_workflow.assert_awaited_once()
        self.agent.start_and_submit_message.assert_not_awaited()
        self.assertEqual(self.agent.submit_message.await_count, 2)
        self.assertEqual(
            self.agent.submit_message.await_args.args,
            ("echo", {"text": "hé", "count": 2}),
        )
        self.assertNotEqual(
            self.agent.submit_message.await_args.kwargs["update_id"], "stable-request"
        )
        self.temporal.get_workflow_handle.return_value.signal.assert_not_called()

    async def test_interactive_input_wait_is_outside_reply_timeout(self):
        args = self.args("--interactive", "--init-prefix", "")
        args.timeout = 0.01
        self.agent.attach = stream_client(
            [
                envelope(
                    MessageHandlerEnd(output={"text": "Answer"}),
                    message="message",
                    turn="turn",
                ),
                envelope(TurnEnded(), message=None, turn="turn"),
            ]
        ).attach
        descriptor, writer = os.pipe()
        self.addCleanup(os.close, writer)
        reader = self.enterContext(os.fdopen(descriptor))
        asyncio.get_running_loop().call_later(0.03, os.write, writer, b"/exit\n")
        with (
            patch.object(sys, "stdin", reader),
            patch.object(reader, "isatty", return_value=True),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            result = await agent_cli.execute(args)
        self.assertEqual(result["result"], {"text": "Answer"})

    async def test_unsupported_interactive_modes_fail_before_connecting(self):
        for flags in (
            {"detach": True},
            {"dry_run": True},
            {"output": "json"},
        ):
            args = self.args("--interactive", "--init-prefix", "")
            for key, value in flags.items():
                setattr(args, key, value)
            with (
                self.subTest(flags=flags),
                patch.object(sys.stdin, "isatty", return_value=True),
                self.assertRaisesRegex(ValueError, "--interactive"),
            ):
                await agent_cli.execute(args)
        args = self.args("--interactive", "--init-prefix", "")
        with (
            patch.object(sys.stdin, "isatty", return_value=False),
            self.assertRaisesRegex(ValueError, "terminal input"),
        ):
            await agent_cli.execute(args)
        args.document["$defs"]["Message"]["required"] = []
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            self.assertRaisesRegex(ValueError, "required string"),
        ):
            await agent_cli.execute(args)
        self.connect.assert_not_awaited()

    async def test_interactive_timeout_includes_waiting_for_message_acceptance(self):
        args = self.args("--interactive", "--init-prefix", "")
        args.timeout = 0.01

        async def pending(*args, **kwargs):
            await asyncio.Event().wait()

        self.agent.submit_message.side_effect = pending
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            redirect_stderr(io.StringIO()),
            self.assertRaises(TimeoutError),
        ):
            await agent_cli.execute(args)

    async def test_interactive_starts_idle_session_before_asking_for_first_message(
        self,
    ):
        args = self.args("--interactive", "--init-prefix", "prefix:")
        delattr(args, "message:text")
        output = io.StringIO()
        args._ui = TerminalChat("Chat", console=Console(file=output))
        self.addCleanup(args._ui.flush)

        async def leave(prompt):
            self.temporal.start_workflow.assert_awaited_once()
            self.agent.submit_message.assert_not_awaited()
            self.agent.start_and_submit_message.assert_not_awaited()
            self.assertIsNone(args._ui.live)
            return "/exit\n"

        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(agent_cli, "read_line", new=AsyncMock(side_effect=leave)),
            redirect_stderr(io.StringIO()),
        ):
            result = await agent_cli.execute(args)
        self.assertEqual(result, {"session": args.session})
        self.assertEqual(
            self.temporal.start_workflow.await_args.kwargs["args"][1],
            {"prefix": "prefix:"},
        )
        self.assertIn("Session", output.getvalue())
        self.assertNotIn("Working", output.getvalue())
        self.temporal.get_workflow_handle.return_value.signal.assert_not_called()

    async def test_interactive_can_open_existing_session_without_sending_message(self):
        args = self.args("--interactive", "--session", "existing")
        delattr(args, "message:text")
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(agent_cli, "read_line", new=AsyncMock(return_value="")),
            redirect_stderr(io.StringIO()),
        ):
            result = await agent_cli.execute(args)
        self.assertEqual(result, {"session": "existing"})
        self.temporal.get_workflow_handle.return_value.describe.assert_awaited_once()
        self.temporal.start_workflow.assert_not_awaited()
        self.agent.submit_message.assert_not_awaited()
        self.agent.attach.assert_awaited_once()
        self.assertEqual(self.agent.attach.call_args.kwargs["from_offset"], 0)

    async def test_resume_restores_transcript_before_input_and_continues_once(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                args = self.args("--interactive", "--session", "existing")
                delattr(args, "message:text")
                output = io.StringIO()
                if terminal:
                    args._ui = TerminalChat(
                        "Chat", console=Console(file=output, width=80)
                    )
                history = stream_client(
                    [
                        envelope(
                            MessageAccepted(
                                handler="echo",
                                payload={"text": "First question"},
                                disposition=MessageDisposition.OPENED,
                            )
                        ),
                        envelope(ReplyDelta(text="Historical answer")),
                        envelope(
                            MessageHandlerEnd(output={"text": "Historical answer"})
                        ),
                        envelope(
                            MessageHandlerEnd(output={"text": "Child detail"}),
                            agent="child",
                        ),
                        envelope(TurnEnded(), message=None),
                        envelope(
                            MessageAccepted(
                                handler="echo",
                                payload={"text": "Second question"},
                                disposition=MessageDisposition.OPENED,
                            ),
                            message="second",
                        ),
                        envelope(
                            MessageHandlerError(message="Past failure"),
                            message="second",
                        ),
                        envelope(TurnEnded(), message=None),
                        envelope(
                            MessageAccepted(
                                handler="echo",
                                payload={"text": "Third question"},
                                disposition=MessageDisposition.OPENED,
                            ),
                            message="third",
                        ),
                        envelope(
                            MessageHandlerEnd(output={"text": "Historical answer"}),
                            message="third",
                        ),
                        envelope(TurnEnded(), message=None),
                    ]
                )
                live = stream_client(
                    [
                        envelope(
                            ReplyDelta(text="New answer"),
                            message="message",
                            turn="turn",
                        ),
                        envelope(
                            MessageHandlerEnd(output={"text": "New answer"}),
                            message="message",
                            turn="turn",
                        ),
                        envelope(TurnEnded(), message=None, turn="turn"),
                    ]
                )
                streams = iter((history, live))

                async def attach(**kwargs):
                    return await next(streams).attach(**kwargs)

                first_input = True

                async def read(prompt):
                    nonlocal first_input
                    if not first_input:
                        return ""
                    first_input = False
                    rendered = output.getvalue()
                    for text in (
                        "First question",
                        "Second question",
                        "Third question",
                        "Past failure",
                    ):
                        self.assertEqual(rendered.count(text), 1)
                    self.assertEqual(rendered.count("Historical answer"), 2)
                    self.assertNotIn("Child detail", rendered)
                    self.assertNotIn("Working", rendered)
                    self.agent.submit_message.assert_not_awaited()
                    return "Next question\n"

                self.agent.workflow_id = "existing"
                self.agent.attach = AsyncMock(side_effect=attach)
                self.agent.submit_message.reset_mock()
                with (
                    patch.object(sys.stdin, "isatty", return_value=True),
                    patch.object(
                        agent_cli, "read_line", new=AsyncMock(side_effect=read)
                    ),
                    redirect_stdout(output),
                    redirect_stderr(output),
                ):
                    await agent_cli.execute(args)
                self.assertEqual(output.getvalue().count("New answer"), 1)
                self.agent.submit_message.assert_awaited_once_with(
                    "echo", {"text": "Next question"}, update_id="stable-request"
                )
                self.assertEqual(history.attach.call_args.kwargs["from_offset"], 0)

    async def test_interactive_still_validates_other_required_fields_before_starting(
        self,
    ):
        args = self.args("--interactive", "--init-prefix", "")
        delattr(args, "message:text")
        args.document["$defs"]["Message"]["required"].append("count")
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            self.assertRaisesRegex(ValueError, "count"),
        ):
            await agent_cli.execute(args)
        self.connect.assert_not_awaited()

    async def test_exit_prints_a_copyable_resume_command_without_resending_input(self):
        registry = self.registry.with_name("agent registry.toml")
        registry.write_text(self.registry.read_text())
        args = self.args(
            "--interactive",
            "--registry",
            str(registry),
            "--session",
            "existing session; echo hello",
            "--profile",
            "my profile",
            "--address",
            "server:7233",
            "--namespace",
            "my namespace",
            "--timeout",
            "30",
            "--count",
            "2",
        )
        self.agent.workflow_id = args.session
        self.agent.attach = stream_client(
            [
                envelope(
                    MessageHandlerEnd(output={"text": "Answer"}),
                    message="message",
                    turn="turn",
                ),
                envelope(TurnEnded(), message=None, turn="turn"),
            ]
        ).attach
        output = io.StringIO()
        with (
            patch.object(sys.stdin, "isatty", return_value=True),
            patch.object(agent_cli, "read_line", new=AsyncMock(return_value="/exit\n")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(output),
        ):
            await agent_cli.execute(args)
        command = output.getvalue().split("Resume with:\n", 1)[1].strip()
        tokens = shlex.split(command)
        self.assertTrue(Path(tokens[0]).is_file())
        start = 3 if tokens[1:3] == ["-m", "temporal_agent_harness.agent_cli"] else 1
        resumed = agent_cli.parse_args(tokens[start:])
        self.assertEqual(resumed.session, args.session)
        self.assertEqual(resumed.registry, registry.resolve())
        self.assertEqual(resumed.handler, "echo")
        self.assertTrue(resumed.interactive)
        self.assertEqual(resumed.input, {"count": 2})
        self.assertIsNone(resumed.request_id)
        self.assertFalse(hasattr(resumed, "init"))
        for field in ("profile", "address", "namespace", "timeout"):
            self.assertEqual(getattr(resumed, field), getattr(args, field))
        self.agent.submit_message.assert_awaited_once_with(
            "echo", {"text": "hi", "count": 2}, update_id="stable-request"
        )

    def test_installed_resume_command_uses_temporal_and_explicit_registry(self):
        args = self.args("--session", "saved", "--interactive")
        executable = str(Path(sys.executable).with_name("temporal-agent"))
        with (
            patch.object(agent_cli, "default_registry", return_value=args.registry),
            patch.object(
                agent_cli.shutil,
                "which",
                side_effect=lambda name: (
                    executable if name == "temporal-agent" else "/usr/bin/temporal"
                ),
            ),
        ):
            command = agent_cli.resume_command(args, {"text": "Last message"}, "text")
        self.assertEqual(
            shlex.split(command),
            [
                "temporal",
                "agent",
                "cli-example",
                "echo",
                "--registry",
                str(args.registry),
                "--session",
                "saved",
                "--interactive",
            ],
        )

    def test_resume_command_keeps_exact_executable_when_path_selects_another_install(
        self,
    ):
        args = self.args("--session", "saved", "--interactive")
        with (
            patch.object(
                agent_cli.shutil,
                "which",
                return_value="/another/environment/temporal-agent",
            ),
            patch.object(Path, "is_file", return_value=True),
        ):
            command = agent_cli.resume_command(args, {"text": "Last message"}, "text")
        self.assertEqual(
            shlex.split(command)[0],
            str(Path(sys.executable).with_name("temporal-agent")),
        )

    def test_resume_command_uses_current_python_without_an_installed_script(self):
        args = self.args("--session", "saved", "--interactive")
        with (
            patch.object(agent_cli.shutil, "which", return_value=None),
            patch.object(Path, "is_file", return_value=False),
        ):
            command = agent_cli.resume_command(args, {"text": "Last message"}, "text")
        self.assertEqual(
            shlex.split(command)[:3],
            [sys.executable, "-m", "temporal_agent_harness.agent_cli"],
        )


async def test_interactive_chat_restores_durable_history_after_worker_restart(tmp_path):
    registry = write_registry(tmp_path)
    first_output, resumed_output = io.StringIO(), io.StringIO()
    async with await WorkflowEnvironment.start_time_skipping(
        plugins=[AgentHarnessPlugin()]
    ) as env:
        with (
            patch.object(
                agent_cli.Client, "connect", AsyncMock(return_value=env.client)
            ),
            patch.object(sys.stdin, "isatty", return_value=True),
        ):
            args = agent_cli.parse_args(
                [
                    "--registry",
                    str(registry),
                    "cli-example",
                    "echo",
                    "--interactive",
                    "--init-prefix",
                    "prefix:",
                    "--request-id",
                    "first-request",
                ]
            )
            args._ui = TerminalChat("CLI Example", console=Console(file=first_output))
            async with Worker(
                env.client,
                task_queue="cli-example",
                workflows=[CLIExampleAgent],
                max_cached_workflows=0,
            ):
                with (
                    patch.object(
                        agent_cli,
                        "read_line",
                        AsyncMock(side_effect=["one\n", "two\n", "/exit\n"]),
                    ),
                ):
                    first = await agent_cli.execute(args)
            assert first["result"] == {"text": "prefix:two"}
            resumed = agent_cli.parse_args(
                [
                    "--registry",
                    str(registry),
                    "cli-example",
                    "echo",
                    "--interactive",
                    "--session",
                    first["session"],
                    "--request-id",
                    "resume-request",
                ]
            )
            resumed._ui = TerminalChat(
                "CLI Example", console=Console(file=resumed_output)
            )
            first_prompt = True

            async def input_after_history(prompt):
                nonlocal first_prompt
                if not first_prompt:
                    return "/exit\n"
                first_prompt = False
                assert "prefix:one" in resumed_output.getvalue()
                assert "prefix:two" in resumed_output.getvalue()
                return "three\n"

            async with Worker(
                env.client,
                task_queue="cli-example",
                workflows=[CLIExampleAgent],
                max_cached_workflows=0,
            ):
                with (
                    patch.object(
                        agent_cli,
                        "read_line",
                        AsyncMock(side_effect=input_after_history),
                    ),
                ):
                    result = await agent_cli.execute(resumed)
                assert result["result"] == {"text": "prefix:three"}
                stream = await agent_cli.AgentClient(
                    env.client, first["session"]
                ).attach(on_item=lambda envelope, offset: envelope)
                accepted = [
                    envelope
                    async for envelope in stream
                    if envelope.event.type == "message_accepted"
                ]
                assert len(accepted) == 3
                await env.client.get_workflow_handle(first["session"]).signal("close")
    for reply in ("prefix:one", "prefix:two", "prefix:three"):
        assert resumed_output.getvalue().count(reply) == 1


@pytest_asyncio.fixture
async def cli_session(tmp_path, monkeypatch):
    registry = write_registry(tmp_path)

    def command(*arguments):
        return agent_cli.parse_args(
            ["--registry", str(registry), "--output", "json", *arguments]
        )

    async with await WorkflowEnvironment.start_time_skipping(
        plugins=[AgentHarnessPlugin()]
    ) as env:
        async with Worker(
            env.client, task_queue="cli-example", workflows=[CLIExampleAgent]
        ):
            monkeypatch.setattr(
                agent_cli.Client, "connect", AsyncMock(return_value=env.client)
            )
            started = await agent_cli.execute(
                command(
                    "cli-example",
                    "echo",
                    "--text",
                    "seed",
                    "--init-prefix",
                    "prefix:",
                    "--request-id",
                    "seed-request",
                )
            )
            client = AgentClient(env.client, started["session"])
            handle = env.client.get_workflow_handle(client.workflow_id)
            try:
                yield command, client, handle
            finally:
                async with asyncio.timeout(30):
                    await handle.signal("close")
                    await handle.result()


async def test_concurrent_cli_messages_keep_their_replies_and_deduplicate_requests(
    cli_session,
):
    command, client, _ = cli_session

    def send(text, request_id):
        return agent_cli.execute(
            command(
                "cli-example",
                "echo",
                "--session",
                client.workflow_id,
                "--text",
                text,
                "--request-id",
                request_id,
            )
        )

    async with asyncio.timeout(30):
        results = await asyncio.gather(
            send("first", "first-request"),
            send("first", "first-request"),
            send("second", "second-request"),
            send("third", "third-request"),
        )
        events = [
            event async for event in await client.attach(on_item=lambda event, _: event)
        ]

    assert [result["result"] for result in results] == [
        {"text": "prefix:first"},
        {"text": "prefix:first"},
        {"text": "prefix:second"},
        {"text": "prefix:third"},
    ]
    completed = [
        event.event.output["text"]
        for event in events
        if event.event.type == "message_handler_end"
    ]
    assert sorted(completed) == [
        "prefix:first",
        "prefix:second",
        "prefix:seed",
        "prefix:third",
    ]


async def test_follow_timeout_leaves_the_turn_running_and_resumable(cli_session):
    command, client, handle = cli_session
    accepted = await agent_cli.execute(
        command(
            "cli-example",
            "wait-for-release",
            "--session",
            client.workflow_id,
            "--text",
            "released",
            "--request-id",
            "waiting-request",
            "--detach",
        )
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            agent_cli.execute(command("follow", client.workflow_id)), timeout=0.05
        )
    assert (await client.get_status()).turn_active

    await handle.signal("release")
    async with asyncio.timeout(30):
        resumed = await agent_cli.execute(command("follow", client.workflow_id))
        events = [
            event async for event in await client.attach(on_item=lambda event, _: event)
        ]

    assert resumed["result"] == {"text": "prefix:released"}
    waiting_messages = [
        event
        for event in events
        if event.event.type == "message_accepted"
        and event.event.handler == "wait_for_release"
    ]
    assert [event.message_id for event in waiting_messages] == [
        accepted["receipt"]["message_id"]
    ]
    assert not (await client.get_status()).turn_active


if __name__ == "__main__":
    unittest.main()
