"""A Claude Agent SDK ``Transport`` for Workflows, which reaches Claude Code through Activities."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import timedelta
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, Transport
from claude_agent_sdk import __version__ as sdk_version
from claude_agent_sdk._internal.transport.subprocess_cli import (
    _SDK_READS_SESSION_STATE_ENV,
    SubprocessCLITransport,
)
from claude_agent_sdk.types import _configure_can_use_tool

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.workflow_streams import WorkflowTopicHandle
from temporalio.exceptions import ApplicationError

from temporal_agent_kit.ai_sdks.claude_agent_sdk._constants import (
    CLOSE,
    CLOSE_OPTIONS,
    CONNECT,
    CONNECT_OPTIONS,
    END_INPUT,
    END_INPUT_OPTIONS,
    GET_QUEUE,
    GET_QUEUE_OPTIONS,
    MAX_WORKER_CONNECT_ATTEMPTS,
    READ,
    READ_OPTIONS,
    STDIN_LABELS,
    WRITE,
    WRITE_OPTIONS,
)
from temporal_agent_kit.ai_sdks.claude_agent_sdk._options import TemporalAgentKitOptions
from temporal_agent_kit.ai_sdks.claude_agent_sdk._types import (
    ConnectActivityInput,
    Output,
    ReadActivityInput,
    ReadResult,
    TransportActivityOptions,
    WriteActivityInput,
)


class TemporalTransport(Transport):
    """A Claude Agent SDK ``Transport`` for Workflows that uses Activities for process I/O."""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        *,
        setup_callback: Callable[[str], Awaitable[None]] | None = None,
        activity_options: TransportActivityOptions | None = None,
        stream: WorkflowTopicHandle[Any] | None = None,
    ) -> None:
        """Prepare the transport. Claude Code starts on ``connect``.

        Args:
            options: The same options passed to ``ClaudeSDKClient``. Configure
                stderr through the Worker's connector. Use ``TemporalAgentKitOptions``
                to publish Claude's stream events to the active Agent Kit turn.
            setup_callback: Optional async Workflow callback that receives the selected
                Worker's task queue. Use it to await setup Activities, such as
                downloading files from S3, before Claude Code starts. Register
                those Activities with ``ClaudeAgentSDKPlugin(setup_activities=[...])``.
            activity_options: Options for each Activity.
            stream: The topic for Claude Code stream events. With ``options.include_partial_messages``,
                read Activities publish Claude's stream events here when there is
                no active Agent Kit turn.
        """
        if options.stderr is not None:
            raise ValueError(
                "Configure stderr through the Worker's connector. "
                "Leave ClaudeAgentOptions.stderr unset in the Workflow."
            )
            
        if not self._is_json_object(options.settings):
            raise ValueError(
                f"{type(self).__name__} needs options.settings to be a JSON "
                "object, not a file path"
            )

        self._options = options
        self._activity_options: TransportActivityOptions = activity_options or {}

        self._task_queue: str | None = None
        self._connected = False

        self._streaming_topic = stream.name if stream is not None else None
        self._runner = options.runner if isinstance(options, TemporalAgentKitOptions) else None

        self._write_lock = asyncio.Lock()

        self._setup_callback = setup_callback

        # Map replay-safe request IDs back to the Claude Agent SDK's original
        # request IDs when the CLI answers.
        self._sdk_ids: dict[str, str] = {}

    async def connect(self) -> None:
        """Get a private task queue, run setup, then start Claude Code."""

        for attempt in range(MAX_WORKER_CONNECT_ATTEMPTS):
            try:
                queue_options: workflow.ActivityConfig = {
                    **GET_QUEUE_OPTIONS,
                    **self._activity_options.get("get_queue", {}),
                    # Retrying a lost reservation response on the shared queue can claim a second host.
                    "retry_policy": RetryPolicy(maximum_attempts=1),
                }

                self._task_queue = await workflow.execute_activity(GET_QUEUE, result_type=str, **queue_options)
                assert self._task_queue is not None

                if self._setup_callback is not None:
                    await self._setup_callback(self._task_queue)

                options: workflow.ActivityConfig = {
                    **CONNECT_OPTIONS,
                    **self._activity_options.get("connect", {}),
                    "task_queue": self._task_queue,
                }

                await workflow.execute_activity(CONNECT, self._connect_input(), **options)
                self._connected = True
                return

            except Exception:
                # The Claude Agent SDK doesn't close a transport whose connect failed.
                # Release the reserved Worker.
                await self.close()

                if attempt == MAX_WORKER_CONNECT_ATTEMPTS - 1:
                    raise

                # Try a different Worker
                await workflow.sleep(timedelta(seconds=10))

    def is_ready(self) -> bool:
        """Return whether the transport has an open connection."""
        return self._connected

    async def write(self, data: str) -> None:
        """Send the Claude Agent SDK's prompts, requests, and answers to the CLI."""
        if not self._connected:
            raise ApplicationError("A connection to a Claude Code process must be established before writing.")

        async with self._write_lock:
            message = json.loads(data)
            kind: str = message["type"]

            # The request id needs to be swapped for a deterministic one for Replay
            if kind == "control_request":
                request_id = self._workflow_uuid()
                self._sdk_ids[request_id] = message["request_id"]
                data = f"{json.dumps({**message, 'request_id': request_id})}\n"
            elif kind == "user" and "uuid" not in message:
                # Claude Code skips a prompt whose uuid it already has, so a
                # retried write doesn't run it again.
                data = f"{json.dumps({**message, 'uuid': self._workflow_uuid()})}\n"

            # Update the stream context for the tasks started once at connect, which keep the first message's
            stream_context = (
                self._runner.current_stream_context
                if kind == "user" and self._runner is not None
                else None
            )
            if stream_context is not None and isinstance(self._options, TemporalAgentKitOptions):
                self._options._stream_context = stream_context

            options: workflow.ActivityConfig = {
                **WRITE_OPTIONS,
                "summary": f"Send {STDIN_LABELS.get(kind, kind)}",
                **self._activity_options.get("write", {}),
                "task_queue": self._task_queue,
            }
            await workflow.execute_activity(
                WRITE,
                WriteActivityInput(text=data, stream_context=stream_context),
                **options,
            )

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield messages from Claude Code as they arrive."""
        if not self._connected:
            raise ApplicationError("A connection to a Claude Code process must be established before reading.")

        options: workflow.ActivityConfig = {
            **READ_OPTIONS,
            **self._activity_options.get("read", {}),
            "task_queue": self._task_queue,
        }

        while True:
            result: ReadResult = await workflow.execute_activity(
                READ,
                ReadActivityInput(streaming_topic=self._streaming_topic),
                result_type=ReadResult,
                **options,
            )

            for message in self._handle_output(result.output):
                yield message

    async def end_input(self) -> None:
        """End the CLI's stdin, after the writes before it."""
        if self._task_queue is None:
                return

        async with self._write_lock:
            options: workflow.ActivityConfig = {
                **END_INPUT_OPTIONS,
                **self._activity_options.get("end_input", {}),
                "task_queue": self._task_queue,
            }

            await workflow.execute_activity(END_INPUT, **options)

    async def close(self) -> None:
        """Stop the CLI and everything it started."""
        if self._task_queue is None:
            return

        options: workflow.ActivityConfig = {
            **CLOSE_OPTIONS,
            **self._activity_options.get("close", {}),
            "task_queue": self._task_queue,
        }

        await workflow.execute_activity(CLOSE, **options)
        self._task_queue = None
        self._connected = False

    def _handle_output(self, outputs: list[Output]) -> Iterator[dict[str, Any]]:
        """Handle a batch of the CLI's output, and yield the messages in it."""
        for output in outputs:
            if output.get("type") != "control_response":
                yield output
                continue

            # Swap the deterministic request ID back to the Claude Agent SDK's.
            # Pass other responses as they are, such as the SDK's own answers that
            # replay-user-messages echoes, which the SDK ignores.
            response = output["response"]
            sdk_id = self._sdk_ids.pop(response["request_id"], None)
            if sdk_id is None:
                yield output
                continue
            
            yield {**output, "response": {**response, "request_id": sdk_id}}

    def _connect_input(self) -> ConnectActivityInput:
        """Claude Code's command line and environment, built from the options as in the Claude Agent SDK."""
        options = _configure_can_use_tool(self._options)
        # The Claude Agent SDK builds the command line only inside its
        # subprocess transport, so that code is borrowed here. Replace this once
        # the Python Claude Agent SDK has a hook like the TypeScript one's
        # spawnClaudeCodeProcess. It needs a CLI path, which only the connector
        # can find, so it gets a placeholder that is then dropped.
        placeholder = dataclasses.replace(options, cli_path="claude")
        args = SubprocessCLITransport(prompt="", options=placeholder)._build_command()[
            1:
        ]

        env = {
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            **options.env,
            "CLAUDE_AGENT_SDK_VERSION": sdk_version,
        }

        # As in the Claude Agent SDK, set it unless options.env has it, in any
        # letter case. It makes Claude Code emit the session_state_changed
        # messages the Claude Agent SDK uses to tell when a run is over.
        if not any(key.upper() == _SDK_READS_SESSION_STATE_ENV for key in env):
            env[_SDK_READS_SESSION_STATE_ENV] = "1"

        if options.enable_file_checkpointing:
            env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"

        return ConnectActivityInput(
            args,
            env,
            cli_path=str(options.cli_path) if options.cli_path else None,
            cwd=str(options.cwd) if options.cwd else None,
            user=options.user,
            max_buffer_size=options.max_buffer_size,
        )

    def _workflow_uuid(self) -> str:
        instance = workflow.instance()
        random = getattr(instance, "__claude_agent_sdk_random", None)
        if random is None:
            random = workflow.new_random("claude-agent-sdk-random")
            setattr(instance, "__claude_agent_sdk_random", random)
        return str(uuid.UUID(int=random.getrandbits(128), version=4))

    def _is_json_object(self, settings: str | None) -> bool:
        """Whether ``settings`` is unset or a JSON object, which the Claude Agent SDK merges without reading a file."""
        if settings is None:
            return True
        try:
            return isinstance(json.loads(settings), dict)
        except ValueError:
            return False
