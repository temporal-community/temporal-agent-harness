"""Python representations of activity payloads scheduled by the Go Teams driver.

The Go workflow worker orchestrates durable delivery, while the Python activity
worker uses the Microsoft Teams SDK for Bot Framework I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ContractError(ValueError):
    """Raised when an activity payload violates the cross-language contract."""


def _string(payload: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = payload.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ContractError(f"{key} must be a string")
    if required and not value:
        raise ContractError(f"{key} is required")
    return value


def parse_conversation(session_id: str) -> str:
    provider, separator, conversation_id = session_id.partition(":")
    if separator != ":" or provider != "teams" or not conversation_id:
        raise ContractError(f'invalid session ID {session_id!r}: expected "teams:<conversationID>" format')
    return conversation_id


@dataclass(frozen=True, slots=True)
class TextMetadata:
    sender_id: str
    session_id: str
    thread_id: str
    text: str
    service_url: str
    channel_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TextMetadata:
        return cls(
            sender_id=_string(payload, "SenderID"),
            session_id=_string(payload, "SessionID", required=True),
            thread_id=_string(payload, "ThreadID"),
            text=_string(payload, "Text"),
            service_url=_string(payload, "ServiceURL"),
            channel_id=_string(payload, "ChannelID"),
        )

    @property
    def conversation_id(self) -> str:
        return parse_conversation(self.session_id)


@dataclass(frozen=True, slots=True)
class StreamHandle:
    id: str
    session_id: str
    transport_mode: str
    task_queue: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> StreamHandle:
        if not isinstance(payload, dict):
            raise ContractError("Handle must be an object")
        return cls(
            id=_string(payload, "ID", required=True),
            session_id=_string(payload, "SessionID", required=True),
            transport_mode=_string(payload, "TransportMode", required=True),
            task_queue=_string(payload, "TaskQueue", required=True),
        )

    def validate_for(self, session_id: str) -> None:
        if self.session_id != session_id:
            raise ContractError("Teams stream handle session does not match input session")


@dataclass(frozen=True, slots=True)
class BeginStream:
    metadata: TextMetadata
    conversation_type: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BeginStream:
        return cls(
            metadata=TextMetadata.from_payload(payload),
            conversation_type=_string(payload, "ConversationType"),
        )


@dataclass(frozen=True, slots=True)
class UpdateStream:
    metadata: TextMetadata
    handle: StreamHandle
    delta: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UpdateStream:
        handle = StreamHandle.from_payload(payload.get("Handle", {}))
        metadata = TextMetadata.from_payload(payload)
        handle.validate_for(metadata.session_id)
        return cls(
            metadata=metadata,
            handle=handle,
            delta=_string(payload, "Delta"),
        )


@dataclass(frozen=True, slots=True)
class FinishStream:
    metadata: TextMetadata
    handle: StreamHandle

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> FinishStream:
        handle = StreamHandle.from_payload(payload.get("Handle", {}))
        metadata = TextMetadata.from_payload(payload)
        handle.validate_for(metadata.session_id)
        return cls(metadata=metadata, handle=handle)


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    metadata: TextMetadata
    tool_id: str
    tool_name: str
    tool_input: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ApprovalPrompt:
        return cls(
            metadata=TextMetadata.from_payload(payload),
            tool_id=_string(payload, "ToolID", required=True),
            tool_name=_string(payload, "ToolName", required=True),
            tool_input=_string(payload, "ToolInput"),
        )


@dataclass(frozen=True, slots=True)
class UpdateMessage:
    metadata: TextMetadata
    message_id: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> UpdateMessage:
        message_id = _string(payload, "MessageID")
        if not message_id:
            message_id = _string(payload, "ActivityID", required=True)
        return cls(
            metadata=TextMetadata.from_payload(payload),
            message_id=message_id,
        )
