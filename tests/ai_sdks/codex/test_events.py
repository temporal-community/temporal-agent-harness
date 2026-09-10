"""Native Codex events stay bounded, correlated, and useful without a CLI/server."""

import json

import pytest

from temporal_agent_harness.ai_sdks.codex._events import (
    MAX_MESSAGES,
    MAX_TEXT_BYTES,
    MAX_TOOLS,
    CodexEventMapper,
    display_text,
)
from temporal_agent_harness.harness.agent_protocol import (
    ReplyDelta,
    ToolEndEvent,
    ToolErrorEvent,
    ToolProgressDelta,
    ToolRequested,
    ToolStartEvent,
)


def item_event(event_type="item.completed", kind="command_execution", item_id="item_1", **fields):
    return {"type": event_type, "item": {"id": item_id, "type": kind, **fields}}


def test_command_lifecycle_deduplicates_snapshots_and_correlates_events():
    published = []
    mapper = CodexEventMapper(published.append, "invocation-1")
    start = item_event("item.started", command="pytest -q", status="in_progress")
    update = item_event("item.updated", aggregated_output="one passed\n")
    done = item_event(aggregated_output="one passed\ntwo passed\n", status="completed", exit_code=0)
    for event in [start, start, update, update, done, done]:
        mapper.on_event(event)
    mapper.finish()
    assert [type(event) for event in published] == [
        ToolRequested, ToolStartEvent, ToolProgressDelta, ToolEndEvent,
    ]
    assert {event.tool_id for event in published} == {"codex:invocation-1:item_1"}
    assert {event.tool_name for event in published} == {"codex.command"}
    assert published[0].tool_input == {"command": "pytest -q"}
    assert published[2].progress_delta == "one passed\n"
    assert published[-1].tool_output == "Exit code: 0\none passed\ntwo passed"


def test_updated_command_output_publishes_only_new_suffix():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    for text in ["first\n", "first\nsecond\n", "first\nsecond\n"]:
        mapper.on_event(item_event("item.updated", aggregated_output=text))
    assert [e.progress_delta for e in published if isinstance(e, ToolProgressDelta)] == [
        "first\n", "second\n",
    ]


@pytest.mark.parametrize("kind, fields, expected", [
    ("file_change", {"changes": [{"path": "src/app.py", "kind": "update", "diff": "SECRET DIFF"}]}, "update: src/app.py"),
    ("mcp_tool_call", {"server": "docs", "tool": "search", "arguments": {"secret": "SECRET ARG"}, "result": "SECRET RESULT"}, "docs / search"),
    ("web_search", {"query": "Python asyncio documentation"}, "Python asyncio documentation"),
    ("todo_list", {"items": [{"text": "Run tests", "completed": True}]}, "[x] Run tests"),
])
def test_completed_only_items_get_full_lifecycle_without_private_payloads(kind, fields, expected):
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event(kind=kind, **fields))
    assert [type(event) for event in published] == [ToolRequested, ToolStartEvent, ToolEndEvent]
    assert published[-1].tool_output == expected
    assert "SECRET" not in json.dumps([e.model_dump() for e in published])
    if kind == "todo_list":
        assert published[0].tool_name == "codex.plan"


def test_plan_updates_have_progress_and_one_terminal_event():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event("item.started", "todo_list", items=[{"text": "Test", "completed": False}]))
    mapper.on_event(item_event("item.updated", "todo_list", items=[{"text": "Test", "completed": True}]))
    mapper.on_event(item_event(kind="todo_list", items=[{"text": "Test", "completed": True}]))
    assert [e.progress_delta for e in published if isinstance(e, ToolProgressDelta)] == ["[ ] Test", "[x] Test"]
    assert sum(isinstance(e, ToolEndEvent) for e in published) == 1


@pytest.mark.parametrize("fields", [
    {"status": "failed"}, {"status": "declined"}, {"status": "cancelled"},
    {"status": "completed", "exit_code": 1},
])
def test_failed_tools_end_with_error(fields):
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event(aggregated_output="test failed", **fields))
    mapper.finish()
    assert isinstance(published[-1], ToolErrorEvent)
    assert "test failed" in published[-1].message
    assert not any(isinstance(e, ToolEndEvent) for e in published)
    # A tool failure can be repaired by Codex; it does not fail the invocation.
    assert mapper.failure is None


def test_unique_namespaces_even_when_ids_contain_delimiters():
    first, second = [], []
    CodexEventMapper(first.append, "a:b").on_event(item_event(item_id="c"))
    CodexEventMapper(second.append, "a").on_event(item_event(item_id="b:c"))
    assert first[0].tool_id != second[0].tool_id


def test_finish_closes_all_active_tools_once_and_stops_future_events():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event("item.started", item_id="one"))
    mapper.on_event(item_event("item.started", kind="web_search", item_id="two"))
    mapper.finish("Cancelled by operator")
    mapper.finish("another error")
    mapper.on_event(item_event(item_id="three"))
    errors = [e for e in published if isinstance(e, ToolErrorEvent)]
    assert len(errors) == 2
    assert {e.message for e in errors} == {"Cancelled by operator"}
    assert mapper.failure == "Cancelled by operator"
    assert not mapper.completed


def test_successful_turn_with_incomplete_tool_does_not_leave_running_card():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event("item.started"))
    mapper.on_event({"type": "turn.completed"})
    mapper.finish()
    assert isinstance(published[-1], ToolErrorEvent)
    assert "before this tool completed" in published[-1].message


@pytest.mark.parametrize("event", [
    {"type": "error", "message": "Unable to authenticate: API_KEY=hide-me"},
    {"type": "turn.failed", "error": {"message": "Unable to authenticate: API_KEY=hide-me"}},
])
def test_terminal_failure_closes_tools_and_cannot_be_overwritten_by_success(event):
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event("item.started"))
    mapper.on_event(event)
    mapper.on_event(event)
    mapper.on_event({"type": "turn.completed"})
    mapper.finish()
    assert len([e for e in published if isinstance(e, ToolErrorEvent)]) == 1
    assert not mapper.completed
    assert mapper.failure and "hide-me" not in mapper.failure
    assert "[redacted]" in mapper.failure


def test_replies_export_completed_snapshots_once_and_omit_reasoning():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event("item.started", "agent_message", text="Hel"))
    mapper.on_event(item_event("item.updated", "agent_message", text="Hello"))
    mapper.on_event(item_event(kind="agent_message", text="Hello there"))
    mapper.on_event(item_event(kind="agent_message", text="Hello there"))
    mapper.on_event(item_event(kind="reasoning", item_id="reasoning", text="PRIVATE REASONING"))
    mapper.on_event(item_event(kind="agent_message", item_id="last", text="All done"))
    assert [e.text for e in published] == ["Hello there", "All done"]
    assert mapper.final_text == "All done"


def test_usage_counts_cached_input_once_and_aggregates_multiple_turns():
    mapper = CodexEventMapper(lambda e: None, "i")
    mapper.on_event({"type": "thread.started", "thread_id": "thread-1"})
    completion = {"type": "turn.completed", "usage": {
        "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }}
    mapper.on_event(completion)
    mapper.on_event(completion)
    assert mapper.usage.total_tokens == 120
    mapper.on_event({"type": "turn.started"})
    mapper.on_event(completion)
    mapper.finish()
    assert mapper.completed
    assert mapper.thread_id == "thread-1"
    assert mapper.usage.model_dump(exclude_none=True) == {
        "input_tokens": 200, "output_tokens": 40, "cached_tokens": 160,
        "thought_tokens": 10, "total_tokens": 240,
    }


def test_invalid_usage_fields_stay_unknown_instead_of_coercing():
    mapper = CodexEventMapper(lambda e: None, "i")
    mapper.on_event({"type": "turn.completed", "usage": {
        "input_tokens": "100", "cached_input_tokens": -1, "output_tokens": True,
        "reasoning_output_tokens": 2**80,
    }})
    assert mapper.usage is None
    assert mapper.completed


@pytest.mark.parametrize("event", [
    None, [], {}, {"type": []}, {"type": "unknown"},
    {"type": "item.started", "item": None},
    {"type": "item.started", "item": {"id": [], "type": "command_execution"}},
    {"type": "item.started", "item": {"id": "x", "type": []}},
    {"type": "item.started", "item": {"id": "x" * 257, "type": "command_execution"}},
    {"type": "thread.started", "thread_id": []},
])
def test_malformed_unknown_events_do_not_prevent_later_valid_events(event):
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(event)
    mapper.on_event(item_event(kind="agent_message", text="Still working"))
    assert published == [ReplyDelta(text="Still working")]


def test_tool_and_message_limits_bound_state_without_abandoning_started_tools():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    for index in range(MAX_TOOLS + 20):
        mapper.on_event(item_event("item.started", item_id=str(index)))
    for index in range(MAX_MESSAGES + 20):
        mapper.on_event(item_event(kind="agent_message", item_id=f"message-{index}", text="Hi"))
    mapper.finish()
    assert len([e for e in published if isinstance(e, ToolRequested)]) == MAX_TOOLS
    assert len([e for e in published if isinstance(e, ToolErrorEvent)]) == MAX_TOOLS
    assert len([e for e in published if isinstance(e, ReplyDelta)]) == MAX_MESSAGES


@pytest.mark.parametrize("source, secret", [
    ("OPENAI_API_KEY=very-private run", "very-private"),
    ("run --token 'very private'", "very private"),
    ('{"password": "very-private"}', "very-private"),
    ("Authorization: Bearer very-private", "very-private"),
    ("https://user:very-private@example.com/path", "very-private"),
    ("ghp_veryPrivateToken123", "ghp_veryPrivateToken123"),
    ("sk-proj-veryPrivateToken123", "sk-proj-veryPrivateToken123"),
    ("-----BEGIN PRIVATE KEY-----\nvery-private\n-----END PRIVATE KEY-----", "very-private"),
])
def test_common_credentials_are_redacted(source, secret):
    result = display_text(source)
    assert secret not in result
    assert "redacted" in result


def test_large_and_unicode_output_is_byte_bounded():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event(command="token" * 100_000, aggregated_output="🏈" * 100_000))
    assert len(published[0].tool_input["command"].encode()) <= MAX_TEXT_BYTES
    assert len(published[-1].tool_output.encode()) <= MAX_TEXT_BYTES
    mapper.on_event(item_event(kind="agent_message", item_id="reply", text="🏈" * 100_000))
    assert len(mapper.final_text.encode()) <= MAX_TEXT_BYTES


def test_huge_lists_and_unexpected_nested_payloads_remain_bounded():
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event(kind="file_change", changes=[{"path": "long" * 1000, "kind": [1]}] * 10000))
    mapper.on_event(item_event(kind="todo_list", item_id="plan", items=[{"text": "step" * 1000, "completed": []}] * 10000))
    mapper.on_event(item_event(kind="mcp_tool_call", item_id="mcp", server={}, tool=[]))
    for event in published:
        if isinstance(event, ToolEndEvent):
            assert len(event.tool_output.encode()) <= MAX_TEXT_BYTES
        if isinstance(event, ToolRequested):
            assert len(json.dumps(event.tool_input).encode()) < MAX_TEXT_BYTES + 1024


@pytest.mark.parametrize("code", [True, [], {}, "1", 10**10000], ids=["bool", "list", "dict", "str", "large"])
def test_malformed_exit_codes_do_not_break_the_event_stream(code):
    published = []
    mapper = CodexEventMapper(published.append, "i")
    mapper.on_event(item_event(exit_code=code))
    mapper.on_event(item_event(kind="agent_message", item_id="reply", text="Done"))
    assert isinstance(published[-2], ToolEndEvent)
    assert published[-1] == ReplyDelta(text="Done")
