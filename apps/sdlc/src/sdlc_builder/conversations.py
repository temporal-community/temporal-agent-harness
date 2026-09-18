"""Deterministic context carried between messages in one task."""

CONTEXT_CHAR_LIMIT = 120_000


def recent_context(history: list[dict[str, str]]) -> tuple[list[dict[str, str]], bool]:
    """Keep the original request and recent exchanges; the full trace stays durable.

    Bound accumulated model input without introducing a model call to summarize
    it. Never cut a JSON tool result in half: omit older whole records instead.
    """
    if sum(len(item["text"]) for item in history) <= CONTEXT_CHAR_LIMIT:
        return [dict(item) for item in history], False
    original = dict(history[0])
    used = len(original["text"])
    recent = []
    for item in reversed(history[1:]):
        if used + len(item["text"]) > CONTEXT_CHAR_LIMIT - 300:
            break
        recent.append(dict(item))
        used += len(item["text"])
    return [
        original,
        {
            "role": "system",
            "text": "Older conversation/tool output was omitted to fit the context budget. The workspace contains prior changes. Re-read relevant files and rerun checks when evidence is missing; do not assume earlier file hashes are current.",
        },
        *reversed(recent),
    ], True
