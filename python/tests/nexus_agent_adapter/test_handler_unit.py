# ABOUTME: Fast, no-server unit tests for handler.py: the contract <-> harness mappings, and
# the generated types' wire shapes.

from __future__ import annotations

import dataclasses
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nexusrpc import HandlerError, HandlerErrorType
from temporalio.contrib.workflow_streams import PollResult
from temporalio.contrib.workflow_streams._types import _WorkflowStreamWireItem
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from temporal_agent_harness.harness.agent_client import MidTurnRejectedError
from temporal_agent_harness.harness.agent_protocol import (
    AGENT_ID_LENGTH,
    AgentConfig,
    AgentMessageReply,
    AgentStatus,
    AutoApprovalCriteria,
    CloseReason,
    MessageDisposition,
    PendingApproval,
    PendingCallback,
    PendingTurn,
    ToolApprovalPolicy,
)
from temporal_agent_harness.harness.agent_protocol.agent_interface import CallbackResultAck
from temporal_agent_harness.nexus_agent_adapter.generated import (
    ApprovalPolicy,
    PollMessagesOutput,
    ProvideCallbackResultInput,
    ProvideCallbackResultInputResult,
    QuerySessionInput,
    SendAgentMessageInput,
    SendAgentMessageInputPayload,
    SendAgentMessageInputStartData,
    StartConfig,
    StartConfigAutoApprovalCriteria,
    StreamItem,
)
from temporal_agent_harness.nexus_agent_adapter.handler import (
    AgentServiceHandler,
    Config,
    _is_workflow_already_completed,
)

_payload_converter = DataConverter.default.payload_converter

_AGENT_ID = "a" * AGENT_ID_LENGTH

_CONFIG = Config(agent_task_queue="agents", workflow_name="Probe", workflow_id_prefix="p-")


def _round_trip(value: object, type_hint: type) -> object:
    """Generated dataclasses don't validate at construction; the payload converter is the step
    that would reject a wrongly built value."""
    [payload] = _payload_converter.to_payloads([value])
    [decoded] = _payload_converter.from_payloads([payload], [type_hint])
    return decoded


def _send_input(**overrides: object) -> SendAgentMessageInput:
    fields: dict[str, object] = {
        "session_id": "s1",
        "msg_type": "ask",
        "payload": SendAgentMessageInputPayload(additional_properties={"text": "hi"}),
    }
    fields.update(overrides)
    return SendAgentMessageInput(**fields)  # type: ignore[arg-type]


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.request_id = "req-1"
    return ctx


def test_provide_callback_result_input_result_round_trips() -> None:
    inp = ProvideCallbackResultInput(
        session_id="s1",
        tool_id="t1",
        result=ProvideCallbackResultInputResult(additional_properties={"ok": True}),
    )
    decoded = _round_trip(inp, ProvideCallbackResultInput)
    assert decoded.result.additional_properties == {"ok": True}


def test_send_agent_message_input_uses_the_contracts_camel_case_names() -> None:
    inp = _send_input(start_config=StartConfig(agent_id="a1", close_after_idle_seconds=5))
    [payload] = _payload_converter.to_payloads([inp])

    assert b'"sessionId"' in payload.data
    assert b'"startConfig"' in payload.data
    assert b'"closeAfterIdleSeconds"' in payload.data
    assert _round_trip(inp, SendAgentMessageInput) == inp


def _pending_callback(output_schema: dict[str, object]) -> PendingCallback:
    return PendingCallback(
        tool_id="t1", tool_name="ask_user", tool_input={}, output_schema=output_schema, turn_number=1
    )


async def _forwarded_callback_result(
    mock_agent_client_cls: MagicMock, output_schema: dict[str, object], sent: dict[str, object]
) -> object:
    mock_agent_client = mock_agent_client_cls.return_value
    mock_agent_client.get_pending_callbacks = AsyncMock(
        return_value=[_pending_callback(output_schema)]
    )
    mock_agent_client.provide_callback_result = AsyncMock(
        return_value=CallbackResultAck(tool_id="t1", accepted=True)
    )
    handler = AgentServiceHandler(client=MagicMock(), config=_CONFIG)

    await handler.provide_callback_result(
        _ctx(),
        ProvideCallbackResultInput(
            session_id="s1",
            tool_id="t1",
            result=ProvideCallbackResultInputResult(additional_properties=sent),
        ),
    )
    return mock_agent_client.provide_callback_result.await_args.kwargs["result"]


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_provide_callback_result_forwards_an_object_result_as_is(
    mock_agent_client_cls: MagicMock,
) -> None:
    forwarded = await _forwarded_callback_result(
        mock_agent_client_cls, {"type": "object", "properties": {}}, {"answer": 42}
    )
    assert forwarded == {"answer": 42}


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_provide_callback_result_unwraps_a_scalar_result(
    mock_agent_client_cls: MagicMock,
) -> None:
    forwarded = await _forwarded_callback_result(
        mock_agent_client_cls, {"type": "string"}, {"value": "Ada Lovelace"}
    )
    assert forwarded == "Ada Lovelace"


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_send_agent_message_starts_the_session_with_the_callers_config_and_data(
    mock_agent_client_cls: MagicMock,
) -> None:
    mock_agent_client = mock_agent_client_cls.return_value
    mock_agent_client.start_and_submit_message = AsyncMock(
        return_value=AgentMessageReply(
            turn_number=3,
            turn_id="turn-3",
            message_id="m-1",
            disposition=MessageDisposition.QUEUED,
            accepted_offset=17,
        )
    )
    handler = AgentServiceHandler(client=MagicMock(), config=_CONFIG)

    out = await handler.send_agent_message(
        _ctx(),
        _send_input(
            start_config=StartConfig(
                approval_policy=ApprovalPolicy(
                    dangerously_skip_all_approvals=False,
                    auto_approve_inherently_safe=True,
                    auto_approve_tools=["search"],
                    auto_mode_enabled=False,
                ),
                auto_approval_criteria=StartConfigAutoApprovalCriteria(
                    additional_properties={"min_confidence": 0.9}
                ),
                agent_id=_AGENT_ID,
                close_after_idle_seconds=60,
            ),
            start_data=SendAgentMessageInputStartData(additional_properties={"city": "Oslo"}),
        ),
    )

    mock_agent_client_cls.assert_called_once_with(handler._client, "p-s1")
    args = mock_agent_client.start_and_submit_message.await_args
    assert args.args == ("ask", {"text": "hi"})
    assert args.kwargs["workflow_name"] == "Probe"
    assert args.kwargs["task_queue"] == "agents"
    assert args.kwargs["start_data"] == {"city": "Oslo"}
    assert args.kwargs["update_id"] == "send-req-1"
    assert args.kwargs["start_config"] == AgentConfig(
        approval_policy=ToolApprovalPolicy(
            auto_approve_inherently_safe=True, auto_approve_tools=frozenset({"search"})
        ),
        auto_approval_criteria=AutoApprovalCriteria(min_confidence=0.9),
        agent_id=_AGENT_ID,
        close_after_idle=timedelta(seconds=60),
    )
    assert (out.turn_number, out.turn_id, out.message_id) == (3, "turn-3", "m-1")
    assert out.disposition == "queued"
    assert out.stream_head_offset == 17


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_send_agent_message_without_start_config_uses_the_agents_defaults(
    mock_agent_client_cls: MagicMock,
) -> None:
    mock_agent_client = mock_agent_client_cls.return_value
    mock_agent_client.start_and_submit_message = AsyncMock(
        return_value=AgentMessageReply(
            turn_number=1, turn_id="t", message_id="m", disposition=MessageDisposition.OPENED
        )
    )
    handler = AgentServiceHandler(client=MagicMock(), config=_CONFIG)

    await handler.send_agent_message(_ctx(), _send_input())

    kwargs = mock_agent_client.start_and_submit_message.await_args.kwargs
    assert kwargs["start_config"] == AgentConfig()
    assert kwargs["start_data"] is None


async def test_send_agent_message_rejects_an_invalid_start_config_as_bad_request() -> None:
    handler = AgentServiceHandler(client=MagicMock(), config=_CONFIG)

    with pytest.raises(HandlerError) as raised:
        await handler.send_agent_message(
            _ctx(), _send_input(start_config=StartConfig(agent_id="Not A Valid Id"))
        )

    assert raised.value.type == HandlerErrorType.BAD_REQUEST
    assert "startConfig" in str(raised.value)


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_send_agent_message_reports_a_mid_turn_rejection_as_a_typed_bad_request(
    mock_agent_client_cls: MagicMock,
) -> None:
    mock_agent_client_cls.return_value.start_and_submit_message = AsyncMock(
        side_effect=MidTurnRejectedError("ask rejects messages while a turn is open")
    )
    handler = AgentServiceHandler(client=MagicMock(), config=_CONFIG)

    with pytest.raises(HandlerError) as raised:
        await handler.send_agent_message(_ctx(), _send_input())

    assert raised.value.type == HandlerErrorType.BAD_REQUEST
    # The contract's promise to callers: they recognize a busy agent by this cause's type.
    cause = raised.value.__cause__
    assert isinstance(cause, ApplicationError)
    assert cause.type == "MidTurnRejected"


@patch("temporal_agent_harness.nexus_agent_adapter.handler.AgentClient")
async def test_query_agent_status_carries_every_status_field(
    mock_agent_client_cls: MagicMock,
) -> None:
    mock_agent_client_cls.return_value.get_status = AsyncMock(
        return_value=AgentStatus(
            agent_id=_AGENT_ID,
            current_turn=4,
            turn_active=True,
            turn_participants=2,
            pending_turns=[
                PendingTurn(turn_number=5, turn_id="t5", message_id="m5", message="{}")
            ],
            pending_approvals=[
                PendingApproval(
                    tool_id="call-1",
                    tool_name="search",
                    tool_input={"q": "x"},
                    turn_number=4,
                    short_id="ab12",
                )
            ],
            approval_policy=ToolApprovalPolicy(auto_approve_tools=frozenset({"b", "a"})),
            has_auto_approval_evaluator=True,
            close_reason=CloseReason.REQUESTED,
        )
    )
    handler = AgentServiceHandler(client=MagicMock(), config=_CONFIG)

    out = await handler.query_agent_status(_ctx(), QuerySessionInput(session_id="s1"))

    assert (out.agent_id, out.current_turn, out.turn_active, out.turn_participants) == (
        _AGENT_ID,
        4,
        True,
        2,
    )
    assert out.pending_turns[0].message_id == "m5"
    approval = out.pending_approvals[0]
    assert (approval.tool_id, approval.short_id) == ("call-1", "ab12")
    assert approval.tool_input.additional_properties == {"q": "x"}
    assert out.approval_policy.auto_approve_tools == ["a", "b"]
    assert out.approval_policy.auto_mode_enabled is False
    assert out.has_auto_approval_evaluator is True
    assert out.close_reason == "requested"


def _handler_with_signal(signal: AsyncMock) -> AgentServiceHandler:
    client = MagicMock()
    client.get_workflow_handle.return_value.signal = signal
    return AgentServiceHandler(client=client, config=_CONFIG)


async def test_close_session_sends_the_close_signal() -> None:
    signal = AsyncMock()
    handler = _handler_with_signal(signal)

    out = await handler.close_session(_ctx(), QuerySessionInput(session_id="s1"))

    handler._client.get_workflow_handle.assert_called_once_with("p-s1")
    signal.assert_awaited_once_with("close")
    assert out.already_closed is False


async def test_close_session_succeeds_when_the_session_already_ended() -> None:
    handler = _handler_with_signal(
        AsyncMock(
            side_effect=RPCError(
                "workflow execution already completed", RPCStatusCode.NOT_FOUND, b""
            )
        )
    )

    out = await handler.close_session(_ctx(), QuerySessionInput(session_id="s1"))

    assert out.already_closed is True


async def test_close_session_reports_an_unknown_session_as_not_found() -> None:
    handler = _handler_with_signal(
        AsyncMock(side_effect=RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""))
    )

    with pytest.raises(HandlerError) as raised:
        await handler.close_session(_ctx(), QuerySessionInput(session_id="s1"))

    assert raised.value.type == HandlerErrorType.NOT_FOUND


def test_is_workflow_already_completed_true() -> None:
    err = MagicMock()
    err.__str__.return_value = "rpc error: workflow execution already completed for id 'x'"
    assert _is_workflow_already_completed(err) is True


def test_is_workflow_already_completed_case_insensitive() -> None:
    err = MagicMock()
    err.__str__.return_value = "Workflow Execution Already Completed"
    assert _is_workflow_already_completed(err) is True


def test_is_workflow_already_completed_false_for_unrelated_error() -> None:
    err = MagicMock()
    err.__str__.return_value = "deadline exceeded"
    assert _is_workflow_already_completed(err) is False


# poll_messages returns the SDK's TemporalOperationResult unchanged, so the poll update's own
# result IS the operation's result: decoded here on the sync path, and by the caller when the
# async callback delivers it. That only works while PollMessagesOutput mirrors WorkflowStream's
# PollResult field for field. If the contract drifts, the async path breaks where nothing can
# catch it, since the server delivers that payload directly and the handler never runs.


def test_poll_result_decodes_as_poll_messages_output() -> None:
    poll_result = PollResult(
        items=[_WorkflowStreamWireItem(topic="turn_events", data="cGF5", offset=7)],
        next_offset=8,
        more_ready=True,
    )

    decoded = _round_trip(poll_result, PollMessagesOutput)

    assert isinstance(decoded, PollMessagesOutput)
    assert decoded.next_offset == 8
    assert decoded.more_ready is True
    assert [(i.topic, i.data, i.offset) for i in decoded.items] == [("turn_events", "cGF5", 7)]
    # Absent on the wire: only the already-completed sync path sets it.
    assert decoded.closed is None


def test_poll_result_and_poll_messages_output_have_the_same_wire_fields() -> None:
    poll_result_fields = {f.name for f in dataclasses.fields(PollResult)}
    output_fields = {f.name for f in dataclasses.fields(PollMessagesOutput)}

    assert poll_result_fields == output_fields - {"closed"}
    assert {f.name for f in dataclasses.fields(_WorkflowStreamWireItem)} == {
        f.name for f in dataclasses.fields(StreamItem)
    }
