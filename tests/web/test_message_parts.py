# ABOUTME: A submitted message without a "type" key used to be a KeyError and a 500, which loses
# the text the person typed and tells the console nothing it can act on. These pin the shorthand
# forms both message endpoints accept, and that a missing type falls back to asking.

from __future__ import annotations

from temporal_agent_harness.web.app import _message_parts


def test_a_bare_string_is_shorthand_for_asking() -> None:
    assert _message_parts("what is the weather") == ("ask", {"text": "what is the weather"})


def test_a_message_without_a_type_is_taken_as_a_question() -> None:
    assert _message_parts({"payload": {"text": "hello"}}) == ("ask", {"text": "hello"})


def test_an_explicit_type_is_kept() -> None:
    assert _message_parts({"type": "slash", "payload": {"name": "set-model"}}) == (
        "slash",
        {"name": "set-model"},
    )


def test_a_missing_or_empty_payload_becomes_an_empty_one() -> None:
    assert _message_parts({"type": "ask"}) == ("ask", {})
    assert _message_parts({"type": "ask", "payload": None}) == ("ask", {})
