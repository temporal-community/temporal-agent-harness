"""Temporal plugin registering worker-owned Codex execution."""

import dataclasses

from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin

from ._activity import CodexExecutor
from ._models import CodexConfig


def _data_converter(converter: DataConverter | None) -> DataConverter:
    if converter is None:
        return DataConverter(payload_converter_class=PydanticPayloadConverter)
    if converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(converter, payload_converter_class=PydanticPayloadConverter)
    return converter


class CodexPlugin(SimplePlugin):
    """Use ``Client.connect(..., plugins=[CodexPlugin(config)])``.

    Workers created from that client inherit the Codex activity automatically.
    Settings and saved CLI authentication stay on the worker. One plugin instance
    serializes its calls against its configured local workspace.
    """

    def __init__(self, config: CodexConfig):
        self.executor = CodexExecutor(config)
        super().__init__(
            name="CodexPlugin", data_converter=_data_converter,
            activities=[self.executor.execute],
        )
