"""Internal types for bounded workflow-stream polls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AGENT_STREAM_POLL_UPDATE = "__temporal_agent_stream_poll"


@dataclass
class AgentStreamPollInput:
    from_offset: int
    topics: list[str]
    timeout_seconds: float


@dataclass
class AgentStreamPollResult:
    items: list[Any]
    more_ready: bool
    next_offset: int
    closed: bool
