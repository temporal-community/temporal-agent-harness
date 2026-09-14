"""Request context shared by A2A frontends and Nexus executors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RequestContext:
    """Transport-neutral request metadata for one A2A operation."""

    request_id: str | None = None
    idempotency_key: str | None = None
    nexus_headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = None
