"""Constants shared across the Claude Agent SDK integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy

STDIN_LABELS: Final[Mapping[str, str]] = {
    "user": "prompt",
    "control_request": "request",
    "control_response": "reply",
}

CONNECT: Final = "claude-agent-sdk-connect"
WRITE: Final = "claude-agent-sdk-write"
END_INPUT: Final = "claude-agent-sdk-end-input"
READ: Final = "claude-agent-sdk-read"
CLOSE: Final = "claude-agent-sdk-close"
GET_QUEUE: Final = "claude-agent-sdk-get-queue"

MAX_WORKER_CONNECT_ATTEMPTS: Final = 3

# Default options for each Activity, which the user's activity options override.

GET_QUEUE_OPTIONS: Final[workflow.ActivityConfig] = {
    "summary": "Reserve Claude Code Worker",
    "schedule_to_close_timeout": timedelta(seconds=30),
    "start_to_close_timeout": timedelta(seconds=10),
}

CONNECT_OPTIONS: Final[workflow.ActivityConfig] = {
    "summary": "Start Claude Code",
    "start_to_close_timeout": timedelta(minutes=5),
    "heartbeat_timeout": timedelta(seconds=10),
    "schedule_to_start_timeout": timedelta(seconds=10),
}

WRITE_OPTIONS: Final[workflow.ActivityConfig] = {
    "start_to_close_timeout": timedelta(minutes=5),
    "heartbeat_timeout": timedelta(seconds=10),
    "schedule_to_start_timeout": timedelta(seconds=10),
}

READ_OPTIONS: Final[workflow.ActivityConfig] = {
    "summary": "Read output",
    "start_to_close_timeout": timedelta(minutes=5),
    "retry_policy": RetryPolicy(backoff_coefficient=1.0),
    "heartbeat_timeout": timedelta(seconds=10),
    "schedule_to_start_timeout": timedelta(seconds=10),
}

END_INPUT_OPTIONS: Final[workflow.ActivityConfig] = {
    "summary": "End input",
    "start_to_close_timeout": timedelta(minutes=5),
    "heartbeat_timeout": timedelta(seconds=10),
    "schedule_to_start_timeout": timedelta(seconds=10),
}

CLOSE_OPTIONS: Final[workflow.ActivityConfig] = {
    "summary": "Close Claude Code",
    "start_to_close_timeout": timedelta(minutes=5),
    "heartbeat_timeout": timedelta(seconds=10),
    "schedule_to_start_timeout": timedelta(seconds=10),
}
