"""Carry ``bytes`` through JSON activity payloads.

Strands content holds raw ``bytes`` in a few well-defined fields: the ``source``
of ``image``, ``document``, ``video`` and ``audio`` blocks, and
``reasoningContent.redactedContent``. The JSON payload converter cannot carry
them as they are: non-UTF-8 bytes fail to serialize and ASCII bytes come back as
``str``. They are therefore tagged as base64, in the format Strands uses for
its own session storage: ``{"__bytes_encoded__": True, "data": "<base64>"}``.

Only those fields are ever tagged or decoded. Everything else (tool input, tool
result JSON, MCP structured content and metadata) is arbitrary JSON, and a
dictionary there that happens to have the marker's keys must reach the model and
the agent unchanged. A generic walk, such as Strands'
``decode_bytes_values``, cannot tell the two apart.

Every function returns a copy and leaves its argument untouched.
"""

import base64
from collections.abc import Callable
from typing import Any

_MARKER = "__bytes_encoded__"
# Content blocks whose ``source`` holds ``bytes``.
_MEDIA_BLOCKS = ("audio", "document", "image", "video")

_Convert = Callable[[Any], Any]


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {_MARKER: True, "data": base64.b64encode(value).decode()}
    return value


def _decode(value: Any) -> Any:
    """Turn a value in a ``bytes`` field back into ``bytes``.

    Besides the tagged form, this accepts the two shapes a ``bytes`` field was
    recorded as before the tagging existed, so that histories recorded then
    still decode: a string (pydantic converter, which writes bytes as UTF-8 and
    whose ``StreamEvent`` type hint used to encode it back) and a list of ints
    (default converter).
    """
    if isinstance(value, dict):
        if value.get(_MARKER) is True and isinstance(value.get("data"), str):
            return base64.b64decode(value["data"])
        return value
    if isinstance(value, str):
        return value.encode()
    if isinstance(value, list) and all(
        isinstance(item, int) and 0 <= item <= 255 for item in value
    ):
        return bytes(value)
    return value


def _map_field(parent: Any, key: str, convert: _Convert) -> Any:
    """Return ``parent`` with ``convert`` applied to ``parent[key]``, if it is there."""
    if isinstance(parent, dict) and key in parent:
        return {**parent, key: convert(parent[key])}
    return parent


def _map_block(block: Any, convert: _Convert) -> Any:
    """Apply ``convert`` to every ``bytes`` field of one content block."""
    if not isinstance(block, dict):
        return block
    mapped = dict(block)
    for media in _MEDIA_BLOCKS:
        if media in mapped:
            mapped[media] = _map_field(
                mapped[media], "source", lambda s: _map_field(s, "bytes", convert)
            )
    if "reasoningContent" in mapped:
        mapped["reasoningContent"] = _map_field(
            mapped["reasoningContent"], "redactedContent", convert
        )
    if "toolResult" in mapped:
        mapped["toolResult"] = _map_tool_result(mapped["toolResult"], convert)
    return mapped


def _map_blocks(blocks: Any, convert: _Convert) -> Any:
    if not isinstance(blocks, list):
        return blocks
    return [_map_block(block, convert) for block in blocks]


def _map_tool_result(result: Any, convert: _Convert) -> Any:
    return _map_field(result, "content", lambda c: _map_blocks(c, convert))


def _map_messages(messages: Any, convert: _Convert) -> Any:
    if not isinstance(messages, list):
        return messages
    return [
        _map_field(message, "content", lambda c: _map_blocks(c, convert))
        for message in messages
    ]


def _map_event(event: Any, convert: _Convert) -> Any:
    """Apply ``convert`` to the one ``bytes`` field of a stream event."""
    return _map_field(
        event,
        "contentBlockDelta",
        lambda block_delta: _map_field(
            block_delta,
            "delta",
            lambda delta: _map_field(
                delta,
                "reasoningContent",
                lambda reasoning: _map_field(reasoning, "redactedContent", convert),
            ),
        ),
    )


def encode_messages(messages: Any) -> Any:
    """Tag the ``bytes`` in a list of Strands messages as base64."""
    return _map_messages(messages, _encode)


def decode_messages(messages: Any) -> Any:
    """Restore the ``bytes`` that ``encode_messages`` tagged."""
    return _map_messages(messages, _decode)


def encode_event(event: Any) -> Any:
    """Tag the ``bytes`` in a model stream event as base64."""
    return _map_event(event, _encode)


def decode_event(event: Any) -> Any:
    """Restore the ``bytes`` that ``encode_event`` tagged in a stream event."""
    return _map_event(event, _decode)


def blank_event(event: Any) -> Any:
    """Empty the ``bytes`` in a model stream event, keeping the field itself."""
    return _map_event(event, lambda value: b"" if isinstance(value, bytes) else value)
