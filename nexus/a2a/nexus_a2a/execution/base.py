"""Execution boundary between A2A semantics and a Temporal calling context."""

from __future__ import annotations

from typing import Any, Protocol

from nexus_a2a.context import RequestContext


class NexusA2AExecutor(Protocol):
    """Execute one A2A operation from a specific Temporal context."""

    async def execute(
        self,
        *,
        service: type[Any] | str,
        endpoint: str,
        operation: Any,
        argument: Any,
        context: RequestContext,
    ) -> Any: ...
