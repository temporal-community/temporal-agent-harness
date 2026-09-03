from __future__ import annotations

from a2a.types import Message, Part, Role, SendMessageRequest
from google.protobuf.struct_pb2 import Struct
from nexus_a2a import a2a_nexus_data_converter
from pydantic import BaseModel
from temporalio.contrib.pydantic import pydantic_data_converter


class ExampleModel(BaseModel):
    value: str


async def test_a2a_messages_use_package_independent_json() -> None:
    request = SendMessageRequest(
        message=Message(
            message_id="message-1",
            task_id="task-1",
            context_id="task-1",
            role=Role.ROLE_USER,
            parts=[Part(text="hello")],
        )
    )

    [payload] = await a2a_nexus_data_converter.encode([request])
    assert payload.metadata["encoding"] == b"json/plain"
    [decoded] = await a2a_nexus_data_converter.decode([payload], [SendMessageRequest])
    assert decoded == request


async def test_a2a_converter_still_decodes_temporal_protobuf_payloads() -> None:
    request = SendMessageRequest(
        message=Message(
            message_id="message-1",
            task_id="task-1",
            role=Role.ROLE_USER,
            parts=[Part(text="hello")],
        )
    )

    [payload] = await pydantic_data_converter.encode([request])
    assert payload.metadata["encoding"] == b"json/protobuf"
    [decoded] = await a2a_nexus_data_converter.decode([payload], [SendMessageRequest])
    assert decoded == request


async def test_non_a2a_protobufs_keep_temporal_protobuf_encoding() -> None:
    value = Struct()
    value.update({"hello": "world"})

    [payload] = await a2a_nexus_data_converter.encode([value])
    assert payload.metadata["encoding"] == b"json/protobuf"
    [decoded] = await a2a_nexus_data_converter.decode([payload], [Struct])
    assert decoded == value


async def test_a2a_converter_preserves_pydantic_round_trip() -> None:
    value = ExampleModel(value="hello")
    [payload] = await a2a_nexus_data_converter.encode([value])
    [decoded] = await a2a_nexus_data_converter.decode([payload], [ExampleModel])
    assert decoded == value
