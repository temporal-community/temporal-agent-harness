"""An agent's default title is its first message, rendered alike by both SDKs.

``typescript/packages/worker/test/agent-protocol/agent-interface.test.ts`` checks the same cases
against the TypeScript SDK's ``titleFromMessage``.
"""

from __future__ import annotations

from typing import Any

import pytest

from temporal_agent_kit.agent_protocol import (
    AGENT_KIT_MEMO_PREFIX,
    TITLE_MAX_CHARS,
    TITLE_MEMO,
    AgentMessage,
    title_from_message,
)


@pytest.mark.parametrize(
    ("handler", "payload", "title"),
    [
        ("ask", {"text": "Seven Nation Army"}, "Seven Nation Army"),
        ("reset", {}, "reset"),
        ("play", {"cell": 5}, "play(cell=5)"),
        ("set", {"b": "it's", "a": [1, None, True]}, "set(a=[1, None, True], b=\"it's\")"),
    ],
)
def test_a_message_renders_as_one_line(handler: str, payload: dict[str, Any], title: str) -> None:
    assert title_from_message(AgentMessage(type=handler, payload=payload)) == title


def test_a_long_message_is_cut_to_the_limit() -> None:
    message = AgentMessage(type="ask", payload={"text": "🌊" * (TITLE_MAX_CHARS + 5)})
    assert title_from_message(message) == "🌊" * TITLE_MAX_CHARS


def test_the_kit_writes_the_title_under_its_own_memo_prefix() -> None:
    assert TITLE_MEMO == "agent_kit.title"
    assert TITLE_MEMO.startswith(AGENT_KIT_MEMO_PREFIX)
