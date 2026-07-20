import pytest

from teams_activity_worker.contracts import BeginStream, ContractError, UpdateStream, parse_conversation


def test_begin_stream_parses_go_json_field_names() -> None:
    request = BeginStream.from_payload(
        {
            "SenderID": "user-1",
            "SessionID": "teams:conversation-1",
            "ThreadID": "message-1",
            "Text": "hello",
            "ServiceURL": "https://example.test/teams/",
            "ChannelID": "msteams",
            "ConversationType": "personal",
            "OperationID": "workflow/turn/begin/0",
        }
    )

    assert request.metadata.conversation_id == "conversation-1"
    assert request.metadata.thread_id == "message-1"
    assert request.conversation_type == "personal"


def test_update_stream_rejects_handle_for_another_session() -> None:
    with pytest.raises(ContractError, match="handle session"):
        UpdateStream.from_payload(
            {
                "SessionID": "teams:conversation-1",
                "Handle": {
                    "ID": "stream-1",
                    "SessionID": "teams:conversation-2",
                    "TransportMode": "native",
                    "NextSequence": 2,
                },
                "FullText": "hello",
                "Sequence": 2,
            }
        )


@pytest.mark.parametrize("session_id", ["", "slack:C1", "teams:", "conversation-1"])
def test_parse_conversation_rejects_invalid_session_id(session_id: str) -> None:
    with pytest.raises(ContractError, match="expected"):
        parse_conversation(session_id)

