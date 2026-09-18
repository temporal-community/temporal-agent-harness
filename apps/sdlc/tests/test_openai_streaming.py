"""Exercise the installed harness observer with events parsed by the actual SDK."""

from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.responses import ResponseFunctionCallArgumentsDoneEvent
from openai_fixture import function_events
from temporal_agent_harness.harness.agent_protocol import (
    ModelInteractionEnded,
    ModelInteractionStarted,
    ToolRequested,
)
from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

from sdlc_builder.integrations.openai_streaming import (
    compatible_harness_observer_factory,
)


@pytest.mark.parametrize("include_opening", [True, False])
async def test_function_call_without_done_name(monkeypatch, include_opening):
    published = []
    lifecycle = []
    interval = timedelta(milliseconds=750)

    class Publisher:
        def publish(self, event):
            published.append(event)

    @asynccontextmanager
    async def publisher_from_activity(context, **kwargs):
        assert context.turn_id == "test-turn"
        assert kwargs == {"batch_interval": interval}
        lifecycle.append("opened")
        try:
            yield Publisher()
        finally:
            lifecycle.append("closed")

    monkeypatch.setattr(
        AgentWorkflowRunner,
        "publisher_from_activity",
        staticmethod(publisher_from_activity),
    )
    encoded = function_events(
        "lookup",
        {"q": "cats"},
        "fixture-model",
        text_config={"format": {"type": "text"}},
        call_id="call_actual",
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=encoded
        )
    )
    token = {
        "context": {"turn_id": "test-turn", "turn_number": 1, "agent_id": "test-agent"},
        "model": "fixture-model",
    }
    saw_done = False
    async with AsyncOpenAI(
        api_key="fixture-only",
        http_client=httpx.AsyncClient(transport=transport),
    ) as client:
        stream = await client.responses.create(
            model="fixture-model", input="Test stream translation", stream=True
        )
        async with (
            stream,
            compatible_harness_observer_factory(token, batch_interval=interval) as obs,
        ):
            async for event in stream:
                if not include_opening and event.type == "response.output_item.added":
                    continue
                before = event.model_dump()
                if isinstance(event, ResponseFunctionCallArgumentsDoneEvent):
                    saw_done = True
                    assert not hasattr(event, "name")
                await obs.on_event(event)
                # Only the observer's copy may gain a fallback field. The event
                # returned to the SDK/Temporal converter is unchanged.
                assert event.model_dump() == before

    assert saw_done
    assert lifecycle == ["opened", "closed"]
    assert isinstance(published[0], ModelInteractionStarted)
    assert isinstance(published[-1], ModelInteractionEnded)
    assert published[-1].usage is not None
    requested = [event for event in published if isinstance(event, ToolRequested)]
    assert len(requested) == 1
    assert requested[0].tool_id == (
        "call_actual" if include_opening else "fc_call_actual"
    )
    assert requested[0].tool_name == ("lookup" if include_opening else "unknown_tool")
    assert requested[0].tool_input == {"q": "cats"}
