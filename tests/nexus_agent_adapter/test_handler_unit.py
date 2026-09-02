# ABOUTME: Fast, no-server unit tests for handler.py's pure helper logic.
#
# Run with: uv run pytest tests/nexus_agent_adapter/test_handler_unit.py -v

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from temporalio.converter import DataConverter

from temporal_agent_harness.harness.agent_protocol.agent_interface import (
    CallbackResultAck,
)
from temporal_agent_harness.nexus_agent_adapter.generated import (
    ProvideCallbackResultInput,
    ProvideCallbackResultInputResult,
)
from temporal_agent_harness.nexus_agent_adapter.handler import (
    HarnessControlServiceHandler,
    _is_workflow_not_found,
)

_payload_converter = DataConverter.default.payload_converter


def _round_trip(value: object, type_hint: type) -> object:
    """nex-gen 0.2.1+ wraps additionalProperties (map-shaped) fields -- like
    ProvideCallbackResultInput.result -- in a named dataclass instead of a plain dict.
    dataclasses don't validate at construction, so a plain dict builds fine and only
    crashes once the real Nexus wire serializes it. Round-tripping through the real
    payload converter is the exact step that would raise."""
    [payload] = _payload_converter.to_payloads([value])
    [decoded] = _payload_converter.from_payloads([payload], [type_hint])
    return decoded


def test_provide_callback_result_input_result_round_trips() -> None:
    inp = ProvideCallbackResultInput(
        session_id="s1",
        tool_id="t1",
        result=ProvideCallbackResultInputResult(additional_properties={"ok": True}),
    )
    decoded = _round_trip(inp, ProvideCallbackResultInput)
    assert decoded.result.additional_properties == {"ok": True}


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_provide_callback_result_unwraps_result_before_forwarding(
    mock_agent_client_cls: MagicMock,
) -> None:
    """input.result arrives as a wrapper, but AgentClient.provide_callback_result
    expects a plain JSON-native value -- proves the handler unwraps it, not just that
    unwrapping logic works in isolation."""
    mock_agent_client = mock_agent_client_cls.return_value
    mock_agent_client.provide_callback_result = AsyncMock(
        return_value=CallbackResultAck(tool_id="t1", accepted=True)
    )
    handler = HarnessControlServiceHandler(client=MagicMock(), config=MagicMock())

    await handler.provide_callback_result(
        MagicMock(),
        ProvideCallbackResultInput(
            session_id="s1",
            tool_id="t1",
            result=ProvideCallbackResultInputResult(
                additional_properties={"answer": 42}
            ),
        ),
    )

    assert mock_agent_client.provide_callback_result.await_args.kwargs["result"] == {
        "answer": 42
    }


def test_is_workflow_not_found_recognizes_temporal_error() -> None:
    err = MagicMock()
    err.__str__.return_value = "workflow not found for ID: missing-agent"
    assert _is_workflow_not_found(err) is True


def test_is_workflow_not_found_false_for_unrelated_error() -> None:
    err = MagicMock()
    err.__str__.return_value = "deadline exceeded"
    assert _is_workflow_not_found(err) is False
