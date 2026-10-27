from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Decision(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DECISION_UNSPECIFIED: _ClassVar[Decision]
    DECISION_APPROVED: _ClassVar[Decision]
    DECISION_DENIED: _ClassVar[Decision]
    DECISION_ESCALATE: _ClassVar[Decision]
    DECISION_RETRY_LATER: _ClassVar[Decision]
DECISION_UNSPECIFIED: Decision
DECISION_APPROVED: Decision
DECISION_DENIED: Decision
DECISION_ESCALATE: Decision
DECISION_RETRY_LATER: Decision

class VerifyToolCallsResponse(_message.Message):
    __slots__ = ("decision", "reason")
    DECISION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    decision: Decision
    reason: str
    def __init__(self, decision: _Optional[_Union[Decision, str]] = ..., reason: _Optional[str] = ...) -> None: ...

class RecordInputEventsResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
