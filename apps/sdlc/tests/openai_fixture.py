"""Responses API wire fixture, consumed through the real OpenAI Agents SDK."""

import json


def response_events(action, model="gpt-5.6-luna", *, text_config=None) -> bytes:
    text = json.dumps(action)
    item = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [
            {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
        ],
    }
    response = {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": "completed",
        "output": [item],
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": False,
        "tools": [],
        "tool_choice": "auto",
        "temperature": 1,
        "top_p": 1,
        # Real structured Responses echo the requested JSON schema. Plain text
        # metadata hides alias-serialization bugs at the Temporal boundary.
        "text": text_config or {"format": {"type": "text"}},
        "truncation": "disabled",
        "usage": {
            "input_tokens": 50,
            "output_tokens": 20,
            "total_tokens": 70,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    events = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "item_id": item["id"],
            "part": {
                "type": "output_text",
                "text": "",
                "annotations": [],
                "logprobs": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "item_id": item["id"],
            "delta": text,
            "logprobs": [],
        },
        {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "item_id": item["id"],
            "text": text,
            "logprobs": [],
        },
        {
            "type": "response.content_part.done",
            "output_index": 0,
            "content_index": 0,
            "item_id": item["id"],
            "part": item["content"][0],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    ).encode()


def function_events(name, arguments, model, *, text_config, call_id="call_fixture"):
    """A real streamed Responses function call, completed on the next SDK turn."""
    base = response_events({}, model, text_config=text_config).decode()
    response = json.loads(
        [line[6:] for line in base.splitlines() if line.startswith("data: ")][-1]
    )["response"]
    item = {
        "type": "function_call",
        "id": "fc_" + call_id,
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }
    response["output"] = [item]
    events = [
        {
            "type": "response.created",
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**item, "status": "in_progress", "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": item["id"],
            "delta": item["arguments"],
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": item["id"],
            # Real Responses streams can omit name here; it is on the opening item.
            "arguments": item["arguments"],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps({**e, 'sequence_number': i})}\n\n"
        for i, e in enumerate(events)
    ).encode()
