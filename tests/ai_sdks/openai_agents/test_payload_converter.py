"""OpenAI field aliases must survive Temporal activity result serialization."""

import json

from openai.types.responses import ResponseCompletedEvent, ResponseStreamEvent
from pydantic import TypeAdapter

from temporal_agent_harness.ai_sdks.openai_agents import OpenAIPayloadConverter


def test_structured_response_metadata_roundtrips_with_wire_aliases():
    event = ResponseCompletedEvent.model_validate(
        {
            "type": "response.completed",
            "sequence_number": 1,
            "response": {
                "id": "resp_test",
                "created_at": 1,
                "object": "response",
                "model": "test-model",
                "output": [],
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
                "status": "completed",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "Action",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"summary": {"type": "string"}},
                        },
                    }
                },
            },
        }
    )
    converter = OpenAIPayloadConverter()
    payload = converter.to_payloads([[event]])[0]
    text_format = json.loads(payload.data)[0]["response"]["text"]["format"]
    assert "schema" in text_format and "schema_" not in text_format
    # Strict union validation must work; falling back can appear to pass in the
    # host interpreter but reconstruct the wrong event type in a workflow sandbox.
    strict = TypeAdapter(list[ResponseStreamEvent]).validate_json(payload.data)
    assert isinstance(strict[0], ResponseCompletedEvent)
    decoded = converter.from_payloads([payload], [list[ResponseStreamEvent]])[0]
    assert decoded[0].response.text.format.schema_ == event.response.text.format.schema_
