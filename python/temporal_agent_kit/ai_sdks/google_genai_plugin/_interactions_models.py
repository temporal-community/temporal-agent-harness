"""Serializable Pydantic models for the Interactions API integration.

Carriers that cross the activity boundary. Mirrors the role of
:mod:`google_genai_plugin._models` for the legacy generate_content path —
the request side has no model of its own (we pass kwargs through as a
``dict[str, Any]`` plus the harness's ``TurnStreamContext``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

__all__ = [
    "_InteractionResult",
]


class _InteractionResult(BaseModel):
    """Serializable activity output: every SSE event from the streamed call.

    ``events`` is the verbatim sequence of ``InteractionSSEEvent`` objects
    yielded by ``client.aio.interactions.create(stream=True)``, serialized
    one-by-one via ``model_dump(exclude_none=True, mode="json")``. The
    workflow-side shim deserializes each entry back into its concrete
    event class so the workflow can iterate the result with exactly the
    same shape as a direct ``async for event in stream`` against the SDK.
    """

    events: list[dict[str, Any]] = []
