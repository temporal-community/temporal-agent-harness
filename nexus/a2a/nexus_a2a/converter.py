"""Cross-SDK payload conversion for the A2A Nexus JSON binding."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from typing import Any, TypeGuard

from google.protobuf.json_format import MessageToDict, Parse
from google.protobuf.message import Message
from temporalio.api.common.v1 import Payload
from temporalio.contrib.pydantic import (
    PydanticJSONPlainPayloadConverter,
    pydantic_data_converter,
)
from temporalio.converter import (
    BinaryProtoPayloadConverter,
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
    JSONPlainPayloadConverter,
    JSONProtoPayloadConverter,
)

_A2A_PROTO_PACKAGES = ("a2a.v1.", "lf.a2a.v1.")


def _is_a2a_message_type(value: Any) -> TypeGuard[type[Message]]:
    return (
        isinstance(value, type)
        and issubclass(value, Message)
        and value.DESCRIPTOR.full_name.startswith(_A2A_PROTO_PACKAGES)
    )


class A2AJSONPlainPayloadConverter(EncodingPayloadConverter):
    """Encode A2A protobufs as their package-independent JSON representation."""

    def __init__(self, fallback: EncodingPayloadConverter) -> None:
        self._fallback = fallback

    @property
    def encoding(self) -> str:
        return "json/plain"

    def to_payload(self, value: Any) -> Payload | None:
        if isinstance(value, Message):
            if _is_a2a_message_type(type(value)):
                data = json.dumps(
                    MessageToDict(value), separators=(",", ":"), sort_keys=True
                ).encode()
                return Payload(metadata={"encoding": b"json/plain"}, data=data)
            return None
        return self._fallback.to_payload(value)

    def from_payload(self, payload: Payload, type_hint: type | None = None) -> Any:
        if _is_a2a_message_type(type_hint):
            return Parse(payload.data.decode(), type_hint())
        return self._fallback.from_payload(payload, type_hint)


def a2a_payload_converters(
    json_converter: EncodingPayloadConverter,
) -> Sequence[EncodingPayloadConverter]:
    """Return default converters with A2A JSON selected before protobuf encoding."""

    defaults = DefaultPayloadConverter.default_encoding_payload_converters
    non_json = [c for c in defaults if not isinstance(c, JSONPlainPayloadConverter)]
    protobuf_index = next(
        i
        for i, converter in enumerate(non_json)
        if isinstance(
            converter,
            (JSONProtoPayloadConverter, BinaryProtoPayloadConverter),
        )
    )
    return (
        *non_json[:protobuf_index],
        A2AJSONPlainPayloadConverter(json_converter),
        *non_json[protobuf_index:],
    )


class A2ANexusPayloadConverter(CompositePayloadConverter):
    """Pydantic-compatible converter with cross-language A2A JSON support."""

    def __init__(self) -> None:
        super().__init__(*a2a_payload_converters(PydanticJSONPlainPayloadConverter()))


a2a_nexus_data_converter: DataConverter = dataclasses.replace(
    pydantic_data_converter,
    payload_converter_class=A2ANexusPayloadConverter,
)
