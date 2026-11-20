from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, Mock

import pytest
from claude_agent_sdk import HookMatcher, Transport
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.testing import ActivityEnvironment

from temporal_agent_kit.agent_protocol import (
    ContextCompactionDelta,
    ModelInteractionEnded,
    ModelInteractionStarted,
    ReplyDelta,
    TextAnnotationDelta,
    ThoughtSummaryDelta,
    TokenUsage,
    ToolInputDelta,
)
from temporal_agent_kit.agent_workflow import AgentWorkflowRunner
from temporal_agent_kit.ai_sdks.claude_agent_sdk import TemporalAgentKitOptions
from temporal_agent_kit.ai_sdks.claude_agent_sdk._activities import ConnectionActivities
from temporal_agent_kit.ai_sdks.claude_agent_sdk._types import (
    ConnectActivityInput,
    Connector,
    ReadActivityInput,
    WriteActivityInput,
)
from temporal_agent_kit.stream_context import TurnStreamContext


async def _open_connection(env: ActivityEnvironment, activities: ConnectionActivities) -> None:
    await env.run(activities.get_available_task_queue)
    await env.run(activities.connect, ConnectActivityInput(args=[], env={}, cli_path=None))


async def test_read_publishes_claude_events_as_agent_stream_items(monkeypatch: pytest.MonkeyPatch) -> None:
    citation = {
        "type": "char_location",
        "cited_text": "Sales rose.",
        "document_index": 0,
        "start_char_index": 0,
        "end_char_index": 11,
    }
    deltas = [
        {"type": "text_delta", "text": "Sales rose."},
        {"type": "input_json_delta", "partial_json": '{"city":'},
        {"type": "thinking_delta", "thinking": "Compare the totals."},
        {"type": "signature_delta", "signature": "opaque-signature"},
        {"type": "citations_delta", "citation": citation},
        {"type": "compaction_delta", "content": "Prior context.", "encrypted_content": "opaque"},
        {"type": "unknown_delta"},
    ]
    events = [
        {
            "type": "message_start",
            "message": {
                "model": "claude-test",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 1,
                },
            },
        },
        *[
            {"type": "content_block_delta", "index": index, "delta": delta}
            for index, delta in enumerate(deltas)
        ],
        {"type": "message_delta", "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]
    output = [{"type": "stream_event", "event": event} for event in events]
    async def read_messages():
        for message in output:
            yield message

    transport = AsyncMock(spec=Transport)
    transport.read_messages = read_messages
    connector = AsyncMock(spec=Connector)
    connector.connect.return_value = transport
    activities = ConnectionActivities(connector, "test")
    env = ActivityEnvironment()
    await _open_connection(env, activities)
    publisher = Mock()
    published_for: list[TurnStreamContext] = []

    @asynccontextmanager
    async def publisher_from_activity(context: TurnStreamContext):
        published_for.append(context)
        yield publisher

    monkeypatch.setattr(AgentWorkflowRunner, "publisher_from_activity", publisher_from_activity)
    streaming_client_factory = Mock()
    monkeypatch.setattr(WorkflowStreamClient, "from_within_activity", streaming_client_factory)
    context = TurnStreamContext(agent_id="agent", turn_id="turn", turn_number=1, message_id="message")
    prompt = WriteActivityInput(text='{"type": "user"}\n', stream_context=context)
    await env.run(activities.write, prompt)
    result = await env.run(activities.read, ReadActivityInput(streaming_topic="claude-events"))

    assert result.output == output
    assert published_for == [context]
    streaming_client_factory.assert_not_called()
    assert [call.args[0] for call in publisher.publish.call_args_list] == [
        ReplyDelta(text="Sales rose."),
        ToolInputDelta(block_index=1, partial_json='{"city":'),
        ThoughtSummaryDelta(delta={"type": "thinking_delta", "text": "Compare the totals."}),
        ThoughtSummaryDelta(delta={"type": "signature_delta", "signature": "opaque-signature"}),
        TextAnnotationDelta(delta={"annotations": [citation]}),
        ContextCompactionDelta(block_index=5, content="Prior context.", encrypted_content="opaque"),
        ModelInteractionEnded(
            usage=TokenUsage(input_tokens=115, output_tokens=42, cached_tokens=100, total_tokens=157)
        ),
    ]
    await env.run(activities.close)


async def test_model_start_hooks_publish_to_runner() -> None:
    @dataclass(kw_only=True)
    class CustomOptions(TemporalAgentKitOptions):
        label: str

    runner = Mock(spec=AgentWorkflowRunner)
    custom_matcher = HookMatcher(hooks=[AsyncMock(return_value={})])
    options = CustomOptions(runner=runner, label="custom", hooks={"UserPromptSubmit": [custom_matcher]})
    copied = replace(options, model="test-model")
    context = TurnStreamContext(agent_id="agent", turn_id="turn", turn_number=2, message_id="second")
    copied._stream_context = context

    assert copied.label == "custom"
    assert copied.include_partial_messages
    assert copied.hooks is not None
    assert set(copied.hooks) == {"UserPromptSubmit", "PostToolBatch"}
    assert copied.hooks["UserPromptSubmit"][0] is custom_matcher
    assert [len(matchers) for matchers in copied.hooks.values()] == [2, 1]
    for matchers in copied.hooks.values():
        matcher = matchers[-1]
        (callback,) = matcher.hooks
        assert await callback(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "test",
                "transcript_path": "",
                "cwd": "",
                "prompt": "",
            },
            None,
            {"signal": None},
        ) == {}
    assert [(call.args[0], call.kwargs) for call in runner.publish.call_args_list] == [
        (ModelInteractionStarted(model="test-model"), {"stream_context": context}),
        (ModelInteractionStarted(model="test-model"), {"stream_context": context}),
    ]


def test_thinking_defaults_to_a_summarized_display() -> None:
    runner = Mock(spec=AgentWorkflowRunner)

    assert TemporalAgentKitOptions(runner=runner).thinking == {"type": "adaptive", "display": "summarized"}
    assert TemporalAgentKitOptions(runner=runner, thinking={"type": "disabled"}).thinking == {"type": "disabled"}
    assert TemporalAgentKitOptions(runner=runner, max_thinking_tokens=1000).thinking is None
