"""Wire-alias compatibility for the published harness 0.4.0 converter.

The source harness includes this fix, but boltzmann must also work with the
published package. Delegate decoding unchanged so existing task histories keep
their original behavior. Remove this shim once the package includes the fix.
"""

from dataclasses import replace
from typing import Any

from pydantic_core import SchemaSerializer
from pydantic_core.core_schema import any_schema
from temporal_agent_harness.ai_sdks.openai_agents import OpenAIPayloadConverter
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, EncodingPayloadConverter


class _AliasedJSON(EncodingPayloadConverter):
    def __init__(self, delegate: EncodingPayloadConverter):
        self.delegate = delegate
        self.serializer = SchemaSerializer(any_schema())

    @property
    def encoding(self) -> str:
        return "json/plain"

    def to_payload(self, value: Any) -> Payload:
        return Payload(
            metadata={"encoding": self.encoding.encode()},
            data=self.serializer.to_json(value, by_alias=True, exclude_unset=True),
        )

    def from_payload(self, payload: Payload, type_hint: type | None = None) -> Any:
        return self.delegate.from_payload(payload, type_hint)


class OpenAIActivityPayloadConverter(OpenAIPayloadConverter):
    def __init__(self):
        super().__init__()
        self.converters[b"json/plain"] = _AliasedJSON(self.converters[b"json/plain"])


def with_wire_aliases(converter: DataConverter | None) -> DataConverter:
    return replace(
        converter or DataConverter(),
        payload_converter_class=OpenAIActivityPayloadConverter,
    )
