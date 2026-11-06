import pytest


@pytest.fixture
def weather_record():
    events = []

    def emit(kind, **fields):
        events.append(
            {
                "agent_id": "agent1",
                "turn_id": "turn1",
                "turn_number": 1,
                "message_id": None if kind in {"turn_started", "turn_end"} else "message1",
                "timestamp": 1700000000 + len(events),
                "event": {"type": kind, **fields},
            }
        )

    emit(
        "message_accepted",
        handler="ask",
        payload={"text": "Weather in Paris?\n"},
        disposition="opened",
    )
    emit("turn_started")
    emit("model_interaction_started", model="gpt-5.1")
    emit("tool_requested", tool_id="call1", tool_name="get_weather", tool_input={"city": "Paris"})
    emit(
        "model_interaction_ended", model="gpt-5.1", usage={"input_tokens": 100, "output_tokens": 10}
    )
    emit("tool_start", tool_id="call1", tool_name="get_weather", tool_input={"city": "Paris"})
    emit(
        "tool_end",
        tool_id="call1",
        tool_name="get_weather",
        tool_output="It's 72°F and sunny in Paris.",
    )
    emit("model_interaction_started", model="gpt-5.1")
    emit("reply_delta", text="It's 72°F ")
    emit("reply_delta", text="and sunny in Paris.")
    emit(
        "model_interaction_ended", model="gpt-5.1", usage={"input_tokens": 120, "output_tokens": 12}
    )
    emit("message_handler_end", output={"text": "It's 72°F and sunny in Paris."})
    emit("turn_end")
    return {
        "schema_version": 1,
        "session_id": "session1",
        "workflow_type": "HelloAgent",
        "harness_version": "0.8.0",
        "instruction": "Weather in Paris?\n",
        "message_id": "message1",
        "events": events,
        "complete": True,
        "error": None,
        "output": {"text": "It's 72°F and sunny in Paris."},
    }
