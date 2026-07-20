# ABOUTME: Regression test for the harness stream-event discriminator surviving a lossy
# (exclude_unset=True) payload converter. Harness events pin `type` to a Literal default that is
# normally left implicit at construction; a converter run with exclude_unset — notably the OpenAI
# Agents plugin's OpenAIPayloadConverter — used to DROP `type`, so the read side could not resolve
# the AgentStreamItem discriminated union (`union_tag_not_found`) and the turn stream broke. The
# StreamEvent base now forces `type` into the model's fields-set; this locks that in.
#
# Run with: uv run pytest tests/harness/test_event_discriminator_serialization.py -v

from __future__ import annotations

import pytest

from temporal_agent_harness.ai_sdks.openai_agents import OpenAIPayloadConverter
from temporal_agent_harness.harness.agent_protocol import (
    AgentEvent,
)
from temporal_agent_harness.harness.agent_protocol.events import (
    AgentStreamItem,
    MessageQueued,
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TextAnnotationDelta,
    ThoughtSummaryDelta,
    ToolEndEvent,
    ToolRequested,
    ToolStartEvent,
    TurnEnded,
    TurnStarted,
)

# Representative events — each constructed WITHOUT an explicit `type=` (the real-world path), so
# the discriminator is only present if the model pins it. Spans the shapes that cross the turn
# stream: the two that actually broke (turn_started / message_queued), tool lifecycle, model
# bracket, and the streaming deltas.
_EVENTS: list[AgentStreamItem] = [
    TurnStarted(user_message="what's the weather in boston?"),
    MessageQueued(user_message="queued one"),
    TurnEnded(),
    ReplyDelta(text="It's sunny"),
    ThoughtSummaryDelta(delta={"summary": "thinking"}),
    TextAnnotationDelta(delta={"annotation": {"url": "https://example.com"}}),
    ToolRequested(tool_id="call_1", tool_name="get_weather", tool_input={"city": "boston"}),
    ToolStartEvent(tool_id="call_1", tool_name="get_weather", tool_input={"city": "boston"}),
    ToolEndEvent(tool_id="call_1", tool_name="get_weather", tool_output="72F sunny"),
    ModelInteractionStarted(model="gpt-5.1"),
    ModelInteractionEnded(model="gpt-5.1"),
]


@pytest.mark.parametrize("event", _EVENTS, ids=lambda e: e.type.value)
def test_discriminator_present_under_exclude_unset_dump(event: AgentStreamItem):
    # The exact serialization the OpenAI plugin's converter performs.
    dumped = event.model_dump(exclude_unset=True)
    assert "type" in dumped, f"{type(event).__name__} dropped its `type` under exclude_unset"
    assert dumped["type"] == event.type


@pytest.mark.parametrize("event", _EVENTS, ids=lambda e: e.type.value)
def test_agent_event_round_trips_through_openai_lossy_converter(event: AgentStreamItem):
    # Reproduce the failing path end to end with the REAL converter that broke: wrap the event in
    # the envelope the turn stream carries, serialize + deserialize through OpenAIPayloadConverter,
    # and assert the AgentStreamItem union still resolves to the right member.
    converter = OpenAIPayloadConverter()
    envelope = AgentEvent(
        agent_id="agent-1",
        turn_id="turn-1",
        turn_number=1,
        timestamp=1234.5,
        event=event,
    )

    payload = converter.to_payload(envelope)
    assert payload is not None
    restored = converter.from_payload(payload, AgentEvent)

    assert isinstance(restored, AgentEvent)
    assert restored.event.type == event.type
    assert type(restored.event) is type(event)
    # The payload survives too, not just the tag.
    assert restored.event == event
