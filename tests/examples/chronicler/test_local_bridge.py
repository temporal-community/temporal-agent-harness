# ABOUTME: Unit tests for the Chronicler local bridge (the callback-tools fulfiller that owns the
# DM's data). Exercises the pure local operations — filesystem, the session registry, recordings,
# and transcript persistence — with no Temporal, no server, and no Gemini (upload_recording, which
# needs the network, is covered end-to-end at runtime, not here). Fast, and covers the
# security-critical path-escape guard and the error-becomes-tool-result contract.
#
# Run with: uv run pytest tests/examples/chronicler/test_local_bridge.py -v

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

from examples.chronicler.local_bridge import execute_callback


def _call(root: Path, tool: str, tool_input: dict[str, Any]) -> tuple[Any, str | None]:
    """Run one callback synchronously (execute_callback is async so the poll loop can await
    Gemini uploads; these tests only touch the synchronous local paths)."""
    return asyncio.run(execute_callback(root, tool, tool_input))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --- filesystem -------------------------------------------------------------


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    result, error = _call(
        tmp_path, "write_file", {"path": "site/index.html", "content": "<h1>hi</h1>"}
    )
    assert error is None and "site/index.html" in result
    assert (tmp_path / "site" / "index.html").read_text() == "<h1>hi</h1>"
    result, error = _call(tmp_path, "read_file", {"path": "site/index.html"})
    assert error is None and result == "<h1>hi</h1>"


def test_write_binary_file_decodes_base64(tmp_path: Path) -> None:
    raw = b"RIFF\x00\x00WAVEfmt "
    result, error = _call(
        tmp_path, "write_binary_file", {"path": "site/audio/recap.wav", "content_base64": _b64(raw)}
    )
    assert error is None and "bytes" in result
    assert (tmp_path / "site" / "audio" / "recap.wav").read_bytes() == raw


def test_grep_skips_binary_files(tmp_path: Path) -> None:
    (tmp_path / "clip.wav").write_bytes(b"\x00\x01\x02RIFF\xff")
    (tmp_path / "notes.md").write_text("match me")
    result, error = _call(tmp_path, "grep", {"pattern": "match"})
    assert error is None and result == ["notes.md:1: match me"]


def test_path_escape_becomes_error_result_not_raise(tmp_path: Path) -> None:
    result, error = _call(tmp_path, "read_file", {"path": "../../etc/passwd"})
    assert result is None and error is not None and "escapes the local root" in error


def test_unknown_tool_becomes_error_result(tmp_path: Path) -> None:
    result, error = _call(tmp_path, "chmod", {"path": "x"})
    assert result is None and error is not None and "does not implement" in error


# --- registry + recordings --------------------------------------------------


def test_save_recording_registers_and_writes_audio(tmp_path: Path) -> None:
    result, error = _call(
        tmp_path,
        "save_recording",
        {"campaign_id": "duskblade", "title": "The Crypt", "audio_base64": _b64(b"WAVEDATA")},
    )
    assert error is None
    assert result["session_id"] == "sample-01"
    assert result["campaign_id"] == "duskblade"
    assert result["transcribed"] is False
    # The bytes land under recordings/ and the registry records the file.
    assert (tmp_path / "recordings" / "sample-01.wav").read_bytes() == b"WAVEDATA"


def test_list_sessions_reflects_registry_and_unregistered(tmp_path: Path) -> None:
    _call(
        tmp_path,
        "save_recording",
        {"campaign_id": "duskblade", "title": "The Crypt", "audio_base64": _b64(b"A")},
    )
    # A raw drop-in recording that isn't registered yet.
    (tmp_path / "recordings" / "dropped.wav").write_bytes(b"B")
    result, error = _call(tmp_path, "list_sessions", {"campaign_id": None})
    assert error is None
    assert [s["title"] for s in result["sessions"]] == ["The Crypt"]
    assert result["unregistered_files"] == ["dropped.wav"]


def test_ingest_registers_dropped_recordings(tmp_path: Path) -> None:
    (tmp_path / "recordings").mkdir()
    (tmp_path / "recordings" / "the_black_bell.wav").write_bytes(b"B")
    result, error = _call(tmp_path, "ingest_sessions", {"campaign_id": "duskblade"})
    assert error is None
    assert result["added"] == ["the_black_bell"]
    assert result["sessions"][0]["title"] == "The Black Bell"


# --- transcript persistence -------------------------------------------------


def _transcript(session_id: str, text: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "model": "gemini-x",
        "duration_s": 1.0,
        "full_text": text,
        "segments": [{"speaker": "DM", "start_s": 0.0, "end_s": 1.0, "text": text}],
    }


def test_transcript_save_read_round_trip_and_transcribed_flag(tmp_path: Path) -> None:
    _call(
        tmp_path,
        "save_recording",
        {"campaign_id": "duskblade", "title": "The Crypt", "audio_base64": _b64(b"A")},
    )
    # Before saving a transcript, the session is not transcribed.
    listed, _ = _call(tmp_path, "list_sessions", {})
    assert listed["sessions"][0]["transcribed"] is False

    result, error = _call(
        tmp_path, "save_transcript", {"transcript": _transcript("sample-01", "DM: hello")}
    )
    assert error is None and "sample-01" in result

    read, error = _call(tmp_path, "read_transcript", {"session_id": "sample-01"})
    assert error is None and read["full_text"] == "DM: hello"

    # Saving the transcript flips `transcribed` in the registry view.
    listed, _ = _call(tmp_path, "list_sessions", {})
    assert listed["sessions"][0]["transcribed"] is True


def test_read_missing_transcript_is_error_result(tmp_path: Path) -> None:
    result, error = _call(tmp_path, "read_transcript", {"session_id": "nope"})
    assert result is None and error is not None and "no saved transcript" in error
