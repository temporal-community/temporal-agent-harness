# ABOUTME: Fast, no-server unit tests for handler.py's pure helper logic.
#
# Run with: uv run pytest tests/nexus_agent_adapter/test_handler_unit.py -v

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nexusrpc import HandlerError, HandlerErrorType

from temporal_agent_harness.harness.agent_client import MalformedMessageError
from temporal_agent_harness.harness.agent_protocol import AgentStatus, PendingApproval
from temporal_agent_harness.nexus_agent_adapter.generated import (
    QuerySessionInput,
    SendAgentMessageInput,
)

from temporal_agent_harness.nexus_agent_adapter.handler import (
    AgentServiceHandler,
    Config,
    _is_workflow_already_completed,
)


def _handler() -> AgentServiceHandler:
    return AgentServiceHandler(
        MagicMock(),
        Config(
            agent_task_queue="agent-tasks",
            workflow_name="ProbeAgent",
            workflow_id_prefix="probe-",
            is_message_queuing_enabled=False,
        ),
    )


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


async def test_send_agent_message_reports_malformed_payload_as_bad_request() -> None:
    handler = _handler()
    agent_client = MagicMock()
    agent_client.start_and_submit_message = AsyncMock(
        side_effect=MalformedMessageError("Payload does not match the ask handler.")
    )

    with patch.object(handler, "_agent_client", return_value=agent_client):
        with pytest.raises(HandlerError) as rejected:
            await handler.send_agent_message(
                SimpleNamespace(request_id="request-7"),
                SendAgentMessageInput(
                    session_id="session-7",
                    msg_type="ask",
                    payload='{"unexpected": true}',
                ),
            )

    assert rejected.value.type is HandlerErrorType.BAD_REQUEST
    assert str(rejected.value) == "Payload does not match the ask handler."


async def test_query_agent_status_exposes_when_an_approval_cannot_be_remembered() -> None:
    handler = _handler()
    agent_client = MagicMock()
    agent_client.get_status = AsyncMock(
        return_value=AgentStatus(
            agent_id="agent-7",
            pending_approvals=[
                PendingApproval(
                    tool_id="tool-7",
                    tool_name="generate_audio",
                    tool_input={"review_id": "review-7"},
                    turn_number=2,
                    remember_allowed=False,
                )
            ],
        )
    )

    with patch.object(handler, "_agent_client", return_value=agent_client):
        status = await handler.query_agent_status(
            SimpleNamespace(request_id="request-7"),
            QuerySessionInput(session_id="session-7"),
        )

    pending = status.pending_approvals[0]
    assert pending.remember_allowed is False
    assert pending.model_dump(by_alias=True)["rememberAllowed"] is False
