# ABOUTME: The per-mutate() recording session — the ordered list of RFC 6902 ops a draft
# tree accumulates, the single flag flip that revokes every draft node on block exit, and
# RFC 6901 pointer-token escaping.

"""The per-``mutate()`` recording session and RFC 6901 pointer helpers."""

from __future__ import annotations

from typing import Any, Final

from .errors import RevokedDraftError

__all__ = ["DraftSession", "escape", "MISSING"]


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<MISSING>"


MISSING: Final = _Missing()


def escape(segment: str) -> str:
    """Escape one RFC 6901 reference token (``~`` -> ``~0``, ``/`` -> ``~1``)."""
    if "~" in segment:
        segment = segment.replace("~", "~0")
    if "/" in segment:
        segment = segment.replace("/", "~1")
    return segment


class DraftSession:
    """Shared by every draft node produced by one ``mutate()`` call.

    Revocation on exit is a single flag flip, never a tree walk.
    """

    __slots__ = ("ops", "alive")

    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []
        self.alive: bool = True

    def record(self, op: str, path: str, value: Any = MISSING) -> None:
        entry: dict[str, Any] = {"op": op, "path": path}
        if value is not MISSING:
            entry["value"] = value
        self.ops.append(entry)

    def check(self) -> None:
        if not self.alive:
            raise RevokedDraftError("draft used after its mutate() block closed")
