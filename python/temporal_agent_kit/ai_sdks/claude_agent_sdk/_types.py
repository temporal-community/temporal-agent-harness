"""The types shared by a :class:`TemporalTransport`, its Activities, and how a Worker reaches Claude Code."""

from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from claude_agent_sdk import Transport

from temporalio import workflow

from temporal_agent_kit.stream_context import TurnStreamContext

Output = dict[str, Any]
"""A message from Claude Code's stdout."""


class TransportActivityOptions(TypedDict, total=False):
    """Options for each Activity a :class:`TemporalTransport` runs, overriding its own.

    Set ``task_queue`` in ``get_queue`` to choose where the queue selection
    Activity runs. All subsequent Activities run on the selected Worker's
    private task queue.
    """

    get_queue: workflow.ActivityConfig
    """Reserves the selected Worker's private task queue."""
    connect: workflow.ActivityConfig
    """Starts Claude Code."""
    write: workflow.ActivityConfig
    """Writes to Claude Code's stdin."""
    end_input: workflow.ActivityConfig
    """Closes Claude Code's stdin."""
    read: workflow.ActivityConfig
    """Waits for Claude Code's output."""
    close: workflow.ActivityConfig
    """Closes Claude Code."""


@dataclass
class ConnectActivityInput:
    """How to start Claude Code, built from the options as in the Claude Agent SDK."""

    args: list[str]
    """Claude Code's flags."""
    env: dict[str, str]
    """The variables to add to Claude Code's environment."""
    cli_path: str | None
    """``options.cli_path``, or ``None`` so the connector finds Claude Code as in the Claude Agent SDK."""
    cwd: str | None = None
    """``options.cwd``."""
    user: str | None = None
    """``options.user``, the user to run Claude Code as."""
    max_buffer_size: int | None = None
    """``options.max_buffer_size``, the most a message from Claude Code can be."""


@dataclass
class WriteActivityInput:
    """The write Activity's input."""

    text: str
    """One or more lines for Claude Code's stdin."""

    stream_context: TurnStreamContext | None = None
    """The Agent Kit stream context."""


@dataclass
class ReadActivityInput:
    """The read Activity's input."""
    streaming_topic: str | None = None


@dataclass
class ReadResult:
    """The read Activity's result."""

    output: list[Output] = field(default_factory=list)
    """The next batch of messages from Claude Code."""


class Connector(Protocol):
    """Starts Claude Code and reaches it, wherever it runs.

    Exceptions from reading the process terminate the read Activity. Temporal
    can retry a read when it exceeds its heartbeat or start-to-close timeout.
    """

    async def connect(self, request: ConnectActivityInput) -> Transport:
        """Start Claude Code and return a connected Claude Agent SDK ``Transport`` to it.

        The plugin holds the returned transport until the ``close`` Activity,
        and runs every Activity that uses it on this Worker.
        """
        ...
