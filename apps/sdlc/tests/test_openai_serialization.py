"""The published-package compatibility layer preserves OpenAI wire aliases."""

import json

from agents import AgentOutputSchema
from openai.types.responses import ResponseCompletedEvent, ResponseStreamEvent
from openai_fixture import response_events
from pydantic import TypeAdapter

from sdlc_builder.integrations.openai_serialization import (
    OpenAIActivityPayloadConverter,
)
from sdlc_builder.models import Action


def test_sdk_structured_completion_roundtrip():
    action = Action(kind="list", summary="Inspect the repository")
    schema = AgentOutputSchema(Action).json_schema()
    encoded = response_events(
        action.model_dump(),
        text_config={
            "format": {
                "type": "json_schema",
                "name": "Action",
                "schema": schema,
                "strict": True,
            }
        },
    )
    events = TypeAdapter(list[ResponseStreamEvent]).validate_python(
        [
            json.loads(line.removeprefix("data: "))
            for line in encoded.decode().splitlines()
            if line.startswith("data: ")
        ]
    )
    converter = OpenAIActivityPayloadConverter()
    # The native ModelActivity rebuilds SDK classes before returning each event;
    # TypeAdapter validation alone leaves their deferred serializers unbuilt.
    for event in events:
        type(event).model_rebuild()
    payload = converter.to_payloads([events])[0]
    persisted_format = json.loads(payload.data)[-1]["response"]["text"]["format"]
    assert persisted_format["schema"] == schema
    assert "schema_" not in persisted_format
    # Require strict validation, bypassing the published converter's lenient
    # fallback which can otherwise select the wrong event inside the sandbox.
    decoded = TypeAdapter(list[ResponseStreamEvent]).validate_json(payload.data)
    assert isinstance(decoded[-1], ResponseCompletedEvent)
    assert (
        Action.model_validate_json(decoded[-1].response.output[0].content[0].text)
        == action
    )
