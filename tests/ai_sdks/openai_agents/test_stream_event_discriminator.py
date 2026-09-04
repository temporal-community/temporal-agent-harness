# ABOUTME: Regression test for the raw OpenAI stream events a workflow gets back from the
# streaming model activity. Serialized live-API payloads do not validate strictly against
# openai's response models, so the plugin's converter falls back to openai's lenient
# construct_type — which needs TResponseStreamEvent's Annotated discriminator to pick the right
# union variant. Temporal resolves an activity's return hint without include_extras, so the
# workflow has to name that type itself; before it did, the terminal response.completed came back
# as the union's first variant and the agent turn died with "Model did not produce a final
# response!". openai memoizes each union's resolved discriminator process-wide, so a worker that
# had really run the activity was accidentally immune; a process that only replays the workflow
# (serving a query against a running agent) starts cold — hence the cache clear here.
#
# Run with: uv run pytest tests/ai_sdks/openai_agents/test_stream_event_discriminator.py -v

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest_asyncio
from agents import ModelSettings, ModelTracing
from openai._models import DISCRIMINATOR_CACHE

from temporalio import activity, workflow
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIPayloadConverter,
)
from temporal_agent_harness.ai_sdks.openai_agents._temporal_model_stub import (
    _TemporalModelStub,
)

_STREAM_EVENTS: list[dict[str, Any]] = [
    # openai marks `name` required on this event, but the live API omits it.
    {
        "type": "response.function_call_arguments.done",
        "arguments": "{}",
        "item_id": "fc_1",
        "output_index": 0,
        "sequence_number": 1,
    },
    # A structured-output response: the converter writes the JSON schema under the field
    # name `schema_`, while openai's model validates it under its alias, `schema`.
    {
        "type": "response.completed",
        "sequence_number": 2,
        "response": {
            "id": "resp_1",
            "created_at": 0.0,
            "model": "gpt-test",
            "object": "response",
            "output": [],
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "Answer",
                    "schema_": {"type": "object", "properties": {}},
                }
            },
        },
    },
]


@activity.defn(name="invoke_model_activity_streaming")
async def streaming_activity_returning_live_payload(
    input: dict[str, Any],
) -> list[dict[str, Any]]:
    return _STREAM_EVENTS


@workflow.defn
class StreamEventTypesWorkflow:
    @workflow.run
    async def run(self) -> list[str]:
        stub = _TemporalModelStub(
            "gpt-test",
            model_params=ModelActivityParameters(
                start_to_close_timeout=timedelta(seconds=10),
                streaming_topic="stream-event-discriminator",
            ),
            agent=None,
        )
        return [
            type(event).__name__
            async for event in stub.stream_response(
                None,
                "hello",
                ModelSettings(),
                [],
                None,
                [],
                ModelTracing.DISABLED,
                previous_response_id=None,
                conversation_id=None,
                prompt=None,
            )
        ]


@pytest_asyncio.fixture
async def client_and_queue():
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=DataConverter(payload_converter_class=OpenAIPayloadConverter)
    )
    task_queue = f"stream-event-discriminator-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[StreamEventTypesWorkflow],
        activities=[streaming_activity_returning_live_payload],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            yield env.client, task_queue
        finally:
            await env.shutdown()


async def test_streamed_events_keep_their_union_variant(client_and_queue):
    client, task_queue = client_and_queue
    DISCRIMINATOR_CACHE.clear()

    event_types = await client.execute_workflow(
        StreamEventTypesWorkflow.run,
        id=f"stream-event-discriminator-{uuid.uuid4()}",
        task_queue=task_queue,
    )

    assert event_types == [
        "ResponseFunctionCallArgumentsDoneEvent",
        "ResponseCompletedEvent",
    ]
