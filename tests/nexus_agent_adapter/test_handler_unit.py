# ABOUTME: Fast, no-server unit tests for handler.py's pure helper logic.
#
# Run with: uv run pytest tests/nexus_agent_adapter/test_handler_unit.py -v

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from temporalio.converter import DataConverter

from temporal_agent_harness.harness.agent_protocol.agent_interface import (
    CallbackResultAck,
)
from temporal_agent_harness.harness.stream_poll import (
    AgentStreamPollItem,
    AgentStreamPollResult,
)
from temporal_agent_harness.nexus_agent_adapter.generated import (
    ProvideCallbackResultInput,
    ProvideCallbackResultInputResult,
    SendAgentMessageInput,
)
from temporal_agent_harness.nexus_agent_adapter.handler import (
    AgentServiceHandler,
    Config,
    _is_workflow_already_completed,
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


def test_agent_stream_poll_result_round_trips() -> None:
    result = AgentStreamPollResult(
        items=[
            AgentStreamPollItem(topic="turn_events", data='{"type":"reply"}', offset=7)
        ],
        more_ready=False,
        next_offset=8,
        closed=True,
    )

    decoded = _round_trip(result, AgentStreamPollResult)

    assert decoded == result


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
    handler = AgentServiceHandler(client=MagicMock(), config=MagicMock())

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


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_send_agent_message_stamps_account_and_lineage_on_new_session(
    mock_agent_client_cls: MagicMock,
) -> None:
    mock_agent_client_cls.return_value.start_and_submit_message = AsyncMock(
        return_value=SimpleNamespace(
            turn_number=1,
            turn_id="turn-1",
            accepted_offset=0,
            pending=False,
        )
    )
    handler = AgentServiceHandler(
        MagicMock(),
        Config(
            agent_task_queue="agents",
            workflow_name="Agent",
            workflow_id_prefix="",
            is_message_queuing_enabled=True,
        ),
    )

    await handler.send_agent_message(
        SimpleNamespace(request_id="request-1"),
        SendAgentMessageInput(
            session_id="session-1",
            msg_type="ask",
            payload='{"text":"hello"}',
            expected_turn=1,
            account_id="account-1",
            registered_agent_id="whimsical-agent",
            delegation_lineage=["nexus-hello"],
            delegation_depth=1,
            max_delegation_depth=5,
        ),
    )

    config = (
        mock_agent_client_cls.return_value.start_and_submit_message.await_args.kwargs[
            "start_config"
        ]
    )
    assert config.account_id == "account-1"
    assert config.registered_agent_id == "whimsical-agent"
    assert config.delegation_lineage == ("nexus-hello",)
    assert config.delegation_depth == 1
    assert config.max_delegation_depth == 5


def test_is_workflow_already_completed_true() -> None:
    err = MagicMock()
    err.__str__.return_value = (
        "rpc error: workflow execution already completed for id 'x'"
    )
    assert _is_workflow_already_completed(err) is True


def test_is_workflow_already_completed_case_insensitive() -> None:
    err = MagicMock()
    err.__str__.return_value = "Workflow Execution Already Completed"
    assert _is_workflow_already_completed(err) is True


def test_is_workflow_already_completed_false_for_unrelated_error() -> None:
    err = MagicMock()
    err.__str__.return_value = "deadline exceeded"
    assert _is_workflow_already_completed(err) is False


def test_is_workflow_not_found_recognizes_temporal_error() -> None:
    err = MagicMock()
    err.__str__.return_value = "workflow not found for ID: missing-agent"
    assert _is_workflow_not_found(err) is True


def test_is_workflow_not_found_false_for_unrelated_error() -> None:
    err = MagicMock()
    err.__str__.return_value = "deadline exceeded"
    assert _is_workflow_not_found(err) is False
