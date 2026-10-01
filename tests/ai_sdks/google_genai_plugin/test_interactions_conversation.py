# ABOUTME: InteractionConversation rebuilds each streamed Gemini reply into the steps it sends
# back on the next call, commits a reply only once its stream completes, and refuses steps it
# cannot carry. Offline: the events are built the way the Temporal stream rehydrates them.

from __future__ import annotations

from typing import Any

import pytest
from google.genai._interactions._models import construct_type
from google.genai._interactions.types import InteractionSSEEvent
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.ai_sdks.google_genai_plugin import InteractionConversation


async def _stream(*events: dict[str, Any]):
    for e in events:
        yield construct_type(type_=InteractionSSEEvent, value=e)


def _completed(interaction_id: str = "int-1") -> dict[str, Any]:
    return {
        "event_type": "interaction.completed",
        "interaction": {"id": interaction_id, "status": "requires_action"},
    }


def _thought(index: int, signature: str) -> list[dict[str, Any]]:
    return [
        {"event_type": "step.start", "index": index, "step": {"type": "thought"}},
        {
            "event_type": "step.delta",
            "index": index,
            "delta": {"type": "thought_signature", "signature": signature},
        },
        {"event_type": "step.stop", "index": index},
    ]


def _call(index: int, call_id: str, arguments: str) -> list[dict[str, Any]]:
    half = len(arguments) // 2
    return [
        {
            "event_type": "step.start",
            "index": index,
            "step": {
                "type": "function_call",
                "id": call_id,
                "name": "run_code",
                "arguments": {},
                "signature": f"sig-{call_id}",
            },
        },
        {
            "event_type": "step.delta",
            "index": index,
            "delta": {"type": "arguments_delta", "arguments": arguments[:half]},
        },
        {
            "event_type": "step.delta",
            "index": index,
            "delta": {"type": "arguments_delta", "arguments": arguments[half:]},
        },
        {"event_type": "step.stop", "index": index},
    ]


def _text(index: int, *chunks: str) -> list[dict[str, Any]]:
    return [
        {"event_type": "step.start", "index": index, "step": {"type": "model_output"}},
        *(
            {"event_type": "step.delta", "index": index, "delta": {"type": "text", "text": c}}
            for c in chunks
        ),
        {"event_type": "step.stop", "index": index},
    ]


async def test_a_function_call_reply_becomes_the_steps_sent_next():
    conversation = InteractionConversation()
    conversation.add_user_text("Handle ticket T-1.")
    reply = await conversation.read_reply(
        _stream(*_thought(0, "t-sig"), *_call(1, "call_1", '{"script": "x = 1"}'), _completed())
    )

    assert reply.text == ""
    assert reply.interaction_id == "int-1"
    assert [(c.id, c.name, c.arguments, c.signature) for c in reply.function_calls] == [
        ("call_1", "run_code", {"script": "x = 1"}, "sig-call_1")
    ]

    conversation.add_function_results(
        [{"type": "function_result", "call_id": "call_1", "name": "run_code", "result": "ok"}]
    )
    assert conversation.steps == [
        {"type": "user_input", "content": [{"type": "text", "text": "Handle ticket T-1."}]},
        {"type": "thought", "signature": "t-sig"},
        {
            "type": "function_call",
            "id": "call_1",
            "name": "run_code",
            "arguments": {"script": "x = 1"},
            "signature": "sig-call_1",
        },
        {"type": "function_result", "call_id": "call_1", "name": "run_code", "result": "ok"},
    ]


async def test_a_text_reply_is_kept_as_model_output():
    conversation = InteractionConversation()
    conversation.add_user_text("Hi")
    reply = await conversation.read_reply(
        _stream(*_thought(0, "s"), *_text(1, "Hel", "lo"), _completed())
    )
    assert reply.text == "Hello" and reply.function_calls == []
    assert conversation.steps[-1] == {
        "type": "model_output",
        "content": [{"type": "text", "text": "Hello"}],
    }


async def test_a_failed_stream_leaves_the_conversation_unchanged():
    conversation = InteractionConversation()
    conversation.add_user_text("Hi")
    before = conversation.steps
    error = {"event_type": "error", "error": {"message": "overloaded", "code": "unavailable"}}
    with pytest.raises(ApplicationError, match="overloaded"):
        await conversation.read_reply(_stream(*_thought(0, "s"), error))
    with pytest.raises(ApplicationError, match="without interaction.completed"):
        await conversation.read_reply(_stream(*_text(0, "partial")))
    assert conversation.steps == before


async def test_a_step_it_cannot_carry_back_is_refused():
    conversation = InteractionConversation()
    search = {
        "event_type": "step.start",
        "index": 0,
        "step": {"type": "google_search_call", "id": "g1", "arguments": {"queries": ["x"]}},
    }
    with pytest.raises(ApplicationError, match="google_search_call") as raised:
        await conversation.read_reply(_stream(search, _completed()))
    assert raised.value.non_retryable
    assert conversation.steps == []


async def test_steps_returns_a_copy():
    conversation = InteractionConversation()
    conversation.add_user_text("Hi")
    conversation.steps.append({"type": "user_input", "content": []})
    assert len(conversation.steps) == 1
