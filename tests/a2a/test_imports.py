"""Regression tests for leaf-level A2A adapter imports."""

from __future__ import annotations

import subprocess
import sys

from a2a.types import Message, Part, Role, SendMessageRequest

from temporal_agent_harness.ai_sdks.openai_agents import OpenAIPayloadConverter


def test_a2a_stream_import_does_not_initialize_a_harness_cycle() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from temporal_agent_harness.a2a.stream import "
            "HARNESS_EVENT_METADATA_KEY",
        ],
        check=True,
    )


def test_openai_payload_converter_includes_a2a_json_support() -> None:
    request = SendMessageRequest(
        message=Message(
            message_id="message-1",
            task_id="task-1",
            context_id="task-1",
            role=Role.ROLE_USER,
            parts=[Part(text="hello")],
        )
    )
    converter = OpenAIPayloadConverter()

    payload = converter.to_payload(request)

    assert payload is not None
    assert payload.metadata["encoding"] == b"json/plain"
    assert converter.from_payload(payload, SendMessageRequest) == request
