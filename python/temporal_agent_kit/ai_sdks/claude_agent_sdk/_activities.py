"""The Activities a :class:`TemporalTransport` runs, which reach Claude Code through a :class:`Connector`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass

from claude_agent_sdk import Transport
from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError
from temporalio.service import PayloadLimitsConfig

from temporal_agent_kit.agent_protocol import (
    ContextCompactionDelta,
    ModelInteractionEnded,
    ReplyDelta,
    TextAnnotationDelta,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolInputDelta,
)
from temporal_agent_kit.agent_workflow import AgentWorkflowRunner, TurnEventPublisher
from temporal_agent_kit.ai_sdks.claude_agent_sdk._constants import CLOSE, CONNECT, END_INPUT, GET_QUEUE, READ, WRITE
from temporal_agent_kit.ai_sdks.claude_agent_sdk._heartbeat_decorator import auto_heartbeater
from temporal_agent_kit.ai_sdks.claude_agent_sdk._types import (
    ConnectActivityInput,
    Connector,
    Output,
    ReadActivityInput,
    ReadResult,
    WriteActivityInput,
)
from temporal_agent_kit.stream_context import TurnStreamContext

@dataclass(frozen=True)
class _ReadTask:
    workflow_run_id: str | None
    activity_id: str
    task: asyncio.Task[list[Output]]

class ConnectionActivities:
    """One Worker's Activities and its single Claude Code connection."""

    def __init__(self, connector: Connector, host_specific_task_queue: str) -> None:
        """Create the Activities, with no Claude Code running.

        Args:
            connector: Starts Claude Code and reaches it.
            host_specific_task_queue: The task queue only this process polls, where the
                setup and connection Activities must run.
        """
        self.host_specific_task_queue = host_specific_task_queue

        self._connector = connector
        self._connection_lock = asyncio.Lock()

        self._claimed = False
        self._transport: Transport | None = None
        self._read_task: _ReadTask | None = None

        self._output: asyncio.Queue[Output] = asyncio.Queue()
        self._output_task: asyncio.Task[None] | None = None
        self._unread: list[Output] = []
        
        self._stream_context: TurnStreamContext | None = None

        # Token counts for the model call in progress. One call's events can
        # arrive across several read Activities, so they're kept here.
        self._usage: dict[str, int] = {}

    @activity.defn(name=GET_QUEUE)
    @auto_heartbeater
    async def get_available_task_queue(self) -> str:
        """Reserve this Worker and return its private task queue."""
        async with self._connection_lock:
            if self._claimed:
                raise ApplicationError("This Worker is already claimed, or connected to another Claude Code process.", non_retryable=True)

            self._claimed = True
            return self.host_specific_task_queue

    @activity.defn(name=CONNECT)
    @auto_heartbeater
    async def connect(self, request: ConnectActivityInput) -> None:
        """Start Claude Code for the reservation."""
        if not self._claimed or self._transport is not None:
            raise ApplicationError(
                "The Worker must be claimed, and not yet connected, before starting Claude Code.",
                non_retryable=True,
            )

        transport = await self._connector.connect(request)
        self._output = asyncio.Queue()
        self._unread = []
        self._output_task = asyncio.create_task(self._buffer_output(transport))
        self._transport = transport

    @activity.defn(name=WRITE)
    @auto_heartbeater
    async def write(self, request: WriteActivityInput) -> None:
        """Write to Claude Code's stdin."""
        transport = self._running_transport()

        if request.stream_context is not None:
            self._stream_context = request.stream_context

        await transport.write(request.text)

    @activity.defn(name=END_INPUT)
    @auto_heartbeater
    async def end_input(self) -> None:
        """Close Claude Code's stdin."""
        await self._running_transport().end_input()

    @activity.defn(name=READ)
    @auto_heartbeater
    async def read(self, request: ReadActivityInput) -> ReadResult:
        """Read Claude Code's output, and return as much as fits Temporal's payload limit."""
        self._running_transport()
        info = activity.info()

        read_task = self._read_task
        if (
            read_task is None
            or read_task.workflow_run_id != info.workflow_run_id
            or read_task.activity_id != info.activity_id
        ):
            read_task = _ReadTask(
                workflow_run_id=info.workflow_run_id,
                activity_id=info.activity_id,
                task=asyncio.create_task(self._next_batch()),
            )
            self._read_task = read_task

        try:
            output = await asyncio.shield(read_task.task)
        except Exception as error:
            raise ApplicationError("Failed to read Claude Code output.", non_retryable=True) from error

        if self._stream_context is not None:
            async with AgentWorkflowRunner.publisher_from_activity(self._stream_context) as publisher:
                for message in output:
                    self._publish_stream_item(message, publisher)
        elif request.streaming_topic is not None:
            stream_events = [message for message in output if message.get("type") == "stream_event"]
            if stream_events:
                async with WorkflowStreamClient.from_within_activity() as client:
                    topic = client.topic(request.streaming_topic)
                    for message in stream_events:
                        topic.publish(message)

        return ReadResult(output=output)

    @activity.defn(name=CLOSE)
    @auto_heartbeater
    async def close(self) -> None:
        """Release the claim, closing the process if it has started."""
        async with self._connection_lock:
            if self._read_task is not None:
                process_read_task = self._read_task.task
                self._read_task = None
                process_read_task.cancel()
                await asyncio.gather(process_read_task, return_exceptions=True)

            if self._transport is not None:
                await self._transport.close()

            if self._output_task is not None:
                self._output_task.cancel()
                await asyncio.gather(self._output_task, return_exceptions=True)
                self._output_task = None

            self._transport = None
            self._claimed = False
            self._stream_context = None

    def _running_transport(self) -> Transport:
        if self._transport is None:
            raise ApplicationError("No Claude Code connection established.", non_retryable=True)
        return self._transport

    async def _buffer_output(self, transport: Transport) -> None:
        async for message in transport.read_messages():
            self._output.put_nowait(message)

    async def _next_batch(self) -> list[Output]:
        """Wait for Claude Code's output, then return what's waiting, up to Temporal's payload limit."""
        if not self._unread:
            self._unread = [await self._output.get()]

        while True:
            try:
                self._unread.append(self._output.get_nowait())
            except asyncio.QueueEmpty:
                batch, self._unread = self._fit_payload(self._unread)
                return batch


    def _publish_stream_item(self, message: Output, publisher: TurnEventPublisher) -> None:
        # Claude Code wraps incremental model events in a stream_event message.
        if message.get("type") != "stream_event":
            return

        event = message.get("event")
        if not isinstance(event, dict):
            return

        if event.get("type") == "message_start":
            self._usage = {}
            message_data = event.get("message") or {}
            self._add_usage(message_data.get("usage"))
            return

        if event.get("type") == "message_delta":
            self._add_usage(event.get("usage"))
            return

        if event.get("type") == "message_stop":
            publisher.publish(ModelInteractionEnded(usage=self._token_usage()))
            self._usage = {}
            return

        if event.get("type") != "content_block_delta":
            return

        # A delta must have a payload to inspect.
        delta = event.get("delta")
        if not isinstance(delta, Mapping):
            return

        delta_type = delta.get("type")
        block_index = event.get("index")

        # Reply text is appended to the visible response.
        if delta_type == "text_delta" and isinstance(text := delta.get("text"), str):
            publisher.publish(ReplyDelta(text=text))
            return

        # Thinking text uses the thought summary's text field.
        if delta_type == "thinking_delta" and isinstance(thinking := delta.get("thinking"), str):
            publisher.publish(ThoughtSummaryDelta(delta={"type": "thinking_delta", "text": thinking}))
            return

        # The signature is opaque metadata for the thinking block.
        if delta_type == "signature_delta" and isinstance(signature := delta.get("signature"), str):
            publisher.publish(ThoughtSummaryDelta(delta={"type": "signature_delta", "signature": signature}))
            return

        # Attach the citation to the streamed reply as an annotation.
        if delta_type == "citations_delta" and isinstance(citation := delta.get("citation"), dict):
            publisher.publish(TextAnnotationDelta(delta={"annotations": [citation]}))
            return

        # Keep partial tool JSON associated with its content block.
        if (
            delta_type == "input_json_delta"
            and isinstance(block_index, int)
            and isinstance(fragment := delta.get("partial_json"), str)
        ):
            publisher.publish(ToolInputDelta(block_index=block_index, partial_json=fragment))
            return

        # Forward compaction summary text and any opaque metadata for its block.
        if delta_type == "compaction_delta" and isinstance(block_index, int):
            content = delta.get("content")
            encrypted_content = delta.get("encrypted_content")
            publisher.publish(
                ContextCompactionDelta(
                    block_index=block_index,
                    content=content if isinstance(content, str) else None,
                    encrypted_content=encrypted_content if isinstance(encrypted_content, str) else None,
                )
            )
            return

    def _add_usage(self, usage: object) -> None:
        """Keep the token counts in a message_start or message_delta usage."""
        if not isinstance(usage, Mapping):
            return
        for key, value in usage.items():
            if isinstance(value, int):
                self._usage[key] = value

    def _token_usage(self) -> TokenUsage | None:
        """Convert Claude's usage to TokenUsage.

        Claude's input_tokens leaves out tokens read from or written to the
        cache, so they're added back to get the whole prompt.
        """
        if not self._usage:
            return None

        input_tokens = (
            self._usage.get("input_tokens", 0)
            + self._usage.get("cache_read_input_tokens", 0)
            + self._usage.get("cache_creation_input_tokens", 0)
        )
        output_tokens = self._usage.get("output_tokens")
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=self._usage.get("cache_read_input_tokens"),
            total_tokens=input_tokens + (output_tokens or 0),
        )

    def _fit_payload(self, outputs: list[Output]) -> tuple[list[Output], list[Output]]:
        """Split outputs into a batch that fits the payload limit and its leftovers.

        A nonempty input always yields at least one output in the batch.
        """
        limit = PayloadLimitsConfig().payloads_warn_size
        size = 2  # The surrounding JSON list.
        for count, output in enumerate(outputs):
            size += len(json.dumps(output).encode()) + (2 if count else 0)  # ", "
            if count and size > limit:
                return outputs[:count], outputs[count:]
        return outputs, []
