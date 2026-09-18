"""Stream-observer compatibility for the published harness 0.4.0.

That observer eagerly reads arguments.done.name even when the opening function
item already supplied the name. Responses streams can omit this field. Adapt
only the observer's copy; the SDK and Temporal payloads keep the original event.
Remove this adapter when the locked harness package includes the source fix.
"""

from datetime import timedelta
from typing import Any

from openai.types.responses import ResponseFunctionCallArgumentsDoneEvent
from temporal_agent_harness.ai_sdks.integration_helpers import StreamObserver
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
)


class _CompatibleObserver:
    def __init__(self, delegate: StreamObserver[Any]):
        self.delegate = delegate

    async def __aenter__(self):
        self.delegate = await self.delegate.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return await self.delegate.__aexit__(exc_type, exc_val, exc_tb)

    async def on_event(self, event: Any) -> None:
        if isinstance(event, ResponseFunctionCallArgumentsDoneEvent) and not getattr(
            event, "name", None
        ):
            # The delegate still prefers the opening item's actual name/call_id.
            # This value is only used when that opening item was also absent.
            event = event.model_copy(update={"name": "unknown_tool"})
        await self.delegate.on_event(event)


def compatible_harness_observer_factory(
    token: Any, *, batch_interval: timedelta | None = None
) -> StreamObserver[Any]:
    return _CompatibleObserver(
        harness_observer_factory(token, batch_interval=batch_interval)
    )
