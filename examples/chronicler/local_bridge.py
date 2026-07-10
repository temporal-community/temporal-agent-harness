"""The Chronicler's local bridge — the callback-tool fulfiller that owns the DM's data.

The Chronicler workflow runs on a Temporal worker (picture an ephemeral k8s pod) with NO disk of
its own. Everything durable lives here, on the DM's machine, under one local root:

    <root>/recordings/     session audio (.wav/.mp3/…) the DM drops in or a sample is saved to
    <root>/sessions.json   the session registry (this bridge owns it)
    <root>/transcripts/    transcripts the agent persists (<session_id>.json)
    <root>/site/           the static campaign site the agent builds

When the model (inside Code Mode, or the SessionScribe conductor) calls a callback tool, the call
pauses in the workflow and shows up under the session's ``pending_callbacks``. THIS process watches
for those and fulfills them locally:

  * filesystem — ls/tree/read_file/write_file/delete_file/grep + write_binary_file (for audio);
  * registry — list_sessions / ingest_sessions / save_recording (scans + maintains sessions.json);
  * audio in — upload_recording uploads a local recording to the Gemini Files API and returns a
    reference, so the worker transcribes from that WITHOUT the audio ever touching the worker;
  * transcripts — save_transcript / read_transcript (the durable source of truth is here).

It does NOT drive the chat — the DM chats and approves in the web UI; this only fulfills callbacks,
so the whole experience stays in the UI while side effects happen on the DM's machine. It talks
only to the packaged HTTP server (no Temporal client), and needs GEMINI_API_KEY for uploads.

Run from the repo root (the honcho `just dev` stack starts one automatically):

    uv run --group examples python -m examples.chronicler.local_bridge --dir examples/chronicler/local
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from google import genai

from .chronicler_models import (
    GeminiFileRef,
    IngestResult,
    SessionList,
    SessionRef,
    Transcript,
)

# Only these session types carry the Chronicler callback tools; others are skipped.
CHRONICLER_WORKFLOW_TYPES = {"ChroniclerAgent", "ChroniclerSubagentAgent"}

# Audio containers Gemini can transcribe. Used to discover recordings under <root>/recordings.
_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".aiff", ".aif"}
_MIME_BY_EXT = {
    ".wav": "audio/wav",
    ".mp3": "audio/mp3",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
}


# ---------------------------------------------------------------------------
# Local layout
# ---------------------------------------------------------------------------


def _recordings_dir(root: Path) -> Path:
    return root / "recordings"


def _registry_path(root: Path) -> Path:
    return root / "sessions.json"


def _transcripts_dir(root: Path) -> Path:
    return root / "transcripts"


# ---------------------------------------------------------------------------
# Generic filesystem operations (relative to the local root, escape-guarded)
# ---------------------------------------------------------------------------


def _resolve(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under the local ``root``, refusing paths that escape it."""
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"path {rel!r} escapes the local root")
    return target


def _ls(root: Path, path: str) -> list[str]:
    directory = _resolve(root, path)
    if not directory.is_dir():
        raise NotADirectoryError(f"{path!r} is not a directory")
    return [
        child.name + "/" if child.is_dir() else child.name
        for child in sorted(directory.iterdir(), key=lambda p: p.name)
    ]


def _tree(root: Path, path: str) -> str:
    base = _resolve(root, path)
    if not base.exists():
        raise FileNotFoundError(f"{path!r} does not exist")
    label = path if path not in (".", "") else "."
    lines = [label if label.endswith("/") or base.is_file() else f"{label}/"]

    def walk(directory: Path, prefix: str) -> None:
        for child in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            marker = "/" if child.is_dir() else ""
            lines.append(f"{prefix}{child.name}{marker}")
            if child.is_dir():
                walk(child, prefix + "  ")

    if base.is_dir():
        walk(base, "  ")
    return "\n".join(lines)


def _read_file(root: Path, path: str) -> str:
    return _resolve(root, path).read_text(encoding="utf-8")


def _write_file(root: Path, path: str, content: str) -> str:
    target = _resolve(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} characters to {path}"


def _write_binary(root: Path, path: str, content_base64: str) -> str:
    target = _resolve(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(content_base64)
    target.write_bytes(data)
    return f"wrote {len(data)} bytes to {path}"


def _delete_file(root: Path, path: str) -> str:
    target = _resolve(root, path)
    if not target.is_file():
        raise FileNotFoundError(f"{path!r} is not a file")
    target.unlink()
    return f"deleted {path}"


def _grep(root: Path, pattern: str, *, max_results: int = 200) -> list[str]:
    regex = re.compile(pattern)
    root_resolved = root.resolve()
    matches: list[str] = []
    for file in sorted(root_resolved.rglob("*")):
        if not file.is_file():
            continue
        rel = file.relative_to(root_resolved).as_posix()
        try:
            text = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # skip binaries / unreadable files
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                if len(matches) >= max_results:
                    return matches
    return matches


# ---------------------------------------------------------------------------
# Session registry (this bridge owns sessions.json) + recordings + transcripts
# ---------------------------------------------------------------------------


def _load_records(root: Path) -> list[dict]:
    path = _registry_path(root)
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("sessions", [])


def _write_records(root: Path, records: list[dict]) -> None:
    path = _registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"sessions": records}, indent=2) + "\n")
    tmp.replace(path)


def _prettify(stem: str) -> str:
    return " ".join(w for w in stem.replace("-", " ").replace("_", " ").split()).title()


def _is_transcribed(root: Path, session_id: str) -> bool:
    return (_transcripts_dir(root) / f"{session_id}.json").exists()


def _session_ref(root: Path, rec: dict) -> SessionRef:
    return SessionRef(
        session_id=rec["session_id"],
        campaign_id=rec["campaign_id"],
        title=rec["title"],
        recorded_at=rec["recorded_at"],
        number=rec["number"],
        transcribed=_is_transcribed(root, rec["session_id"]),
    )


def _unregistered_files(root: Path, records: list[dict]) -> list[str]:
    known = {rec["audio_file"] for rec in records}
    recordings = _recordings_dir(root)
    if not recordings.is_dir():
        return []
    return sorted(
        p.name
        for p in recordings.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS and p.name not in known
    )


def _list_sessions(root: Path, campaign_id: str | None) -> SessionList:
    records = _load_records(root)
    refs = [
        _session_ref(root, rec)
        for rec in sorted(records, key=lambda r: r["number"])
        if not campaign_id or rec["campaign_id"] == campaign_id
    ]
    return SessionList(sessions=refs, unregistered_files=_unregistered_files(root, records))


def _ingest_sessions(root: Path, campaign_id: str) -> IngestResult:
    records = _load_records(root)
    known_files = {rec["audio_file"] for rec in records}
    existing_ids = {rec["session_id"] for rec in records}
    max_number = max((rec["number"] for rec in records), default=0)

    recordings = _recordings_dir(root)
    found = (
        sorted(p for p in recordings.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)
        if recordings.is_dir()
        else []
    )
    added: list[str] = []
    for path in found:
        if path.name in known_files:
            continue
        session_id = path.stem
        while session_id in existing_ids:
            session_id = f"{session_id}-x"
        existing_ids.add(session_id)
        max_number += 1
        records.append(
            {
                "session_id": session_id,
                "campaign_id": campaign_id,
                "title": _prettify(path.stem),
                "recorded_at": datetime.fromtimestamp(path.stat().st_mtime).date().isoformat(),
                "number": max_number,
                "audio_file": path.name,
            }
        )
        added.append(session_id)

    if added:
        _write_records(root, records)
    campaign = [
        _session_ref(root, rec) for rec in records if rec["campaign_id"] == campaign_id
    ]
    campaign.sort(key=lambda r: r.number)
    return IngestResult(added=added, already_registered=len(known_files), sessions=campaign)


def _save_recording(root: Path, campaign_id: str, title: str, audio_base64: str) -> SessionRef:
    records = _load_records(root)
    number = max((r["number"] for r in records), default=0) + 1
    existing_ids = {r["session_id"] for r in records}
    session_id = f"sample-{number:02d}"
    while session_id in existing_ids:
        number += 1
        session_id = f"sample-{number:02d}"

    recordings = _recordings_dir(root)
    recordings.mkdir(parents=True, exist_ok=True)
    audio_file = f"{session_id}.wav"
    (recordings / audio_file).write_bytes(base64.b64decode(audio_base64))

    record = {
        "session_id": session_id,
        "campaign_id": campaign_id,
        "title": title,
        "recorded_at": datetime.now().date().isoformat(),
        "number": number,
        "audio_file": audio_file,
    }
    records.append(record)
    _write_records(root, records)
    return _session_ref(root, record)


def _upload_recording(root: Path, session_id: str) -> GeminiFileRef:
    rec = next((r for r in _load_records(root) if r["session_id"] == session_id), None)
    if rec is None:
        raise ValueError(f"unknown session_id {session_id!r}")
    audio_path = _recordings_dir(root) / rec["audio_file"]
    if not audio_path.is_file():
        raise FileNotFoundError(f"recording missing for {session_id!r}: {audio_path}")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set — the bridge needs it to upload recordings")
    client = genai.Client(api_key=api_key)
    uploaded = client.files.upload(file=str(audio_path))
    while getattr(uploaded, "state", None) and str(uploaded.state) not in (
        "ACTIVE",
        "FileState.ACTIVE",
    ):
        if str(uploaded.state) in ("FAILED", "FileState.FAILED"):
            raise RuntimeError("Gemini file processing failed")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    mime = uploaded.mime_type or _MIME_BY_EXT.get(audio_path.suffix.lower(), "audio/wav")
    return GeminiFileRef(name=uploaded.name, uri=uploaded.uri or "", mime_type=mime)


def _save_transcript(root: Path, transcript_data: dict) -> str:
    transcript = Transcript.model_validate(transcript_data)
    directory = _transcripts_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{transcript.session_id}.json").write_text(
        transcript.model_dump_json(indent=2) + "\n"
    )
    return f"saved transcript for {transcript.session_id}"


def _read_transcript(root: Path, session_id: str) -> Transcript:
    path = _transcripts_dir(root) / f"{session_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no saved transcript for {session_id!r}")
    return Transcript.model_validate_json(path.read_text())


# ---------------------------------------------------------------------------
# Dispatch — one callback tool call, executed locally. Returns JSON-native result or an error.
# ---------------------------------------------------------------------------


async def execute_callback(
    root: Path, tool_name: str, tool_input: dict[str, Any]
) -> tuple[Any, str | None]:
    """Run one callback tool locally. Returns ``(result, None)`` on success or ``(None, error)``
    if the tool is unknown or the operation fails — the error is sent back as the tool's error
    result (the turn continues; the model sees it). Model returns are JSON-native (dicts)."""
    try:
        # Filesystem (synchronous).
        if tool_name == "ls":
            return _ls(root, tool_input["path"]), None
        if tool_name == "tree":
            return _tree(root, tool_input["path"]), None
        if tool_name == "read_file":
            return _read_file(root, tool_input["path"]), None
        if tool_name == "write_file":
            return _write_file(root, tool_input["path"], tool_input["content"]), None
        if tool_name == "write_binary_file":
            return _write_binary(root, tool_input["path"], tool_input["content_base64"]), None
        if tool_name == "delete_file":
            return _delete_file(root, tool_input["path"]), None
        if tool_name == "grep":
            return _grep(root, tool_input["pattern"]), None
        # Registry + transcripts (synchronous; return validated model dicts).
        if tool_name == "list_sessions":
            return _list_sessions(root, tool_input.get("campaign_id")).model_dump(mode="json"), None
        if tool_name == "ingest_sessions":
            return _ingest_sessions(root, tool_input["campaign_id"]).model_dump(mode="json"), None
        if tool_name == "save_recording":
            ref = _save_recording(
                root, tool_input["campaign_id"], tool_input["title"], tool_input["audio_base64"]
            )
            return ref.model_dump(mode="json"), None
        if tool_name == "save_transcript":
            return _save_transcript(root, tool_input["transcript"]), None
        if tool_name == "read_transcript":
            return _read_transcript(root, tool_input["session_id"]).model_dump(mode="json"), None
        # Gemini upload (blocking network + poll) — off the event loop.
        if tool_name == "upload_recording":
            ref = await asyncio.to_thread(_upload_recording, root, tool_input["session_id"])
            return ref.model_dump(mode="json"), None
        return None, f"this bridge does not implement the tool {tool_name!r}"
    except Exception as e:  # noqa: BLE001 — any local failure becomes a tool error result
        return None, f"{type(e).__name__}: {e}"


def _summarize_args(tool_input: dict[str, Any]) -> str:
    """Compact one-line rendering of a call's args for the log (never dumps file/audio bodies)."""
    parts = []
    for key, value in tool_input.items():
        if isinstance(value, str) and (key.endswith("base64") or key == "content"):
            parts.append(f"{key}=<{len(value)} chars>")
        elif isinstance(value, dict):
            parts.append(f"{key}=<{key}>")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


def _result_summary(result: Any) -> str:
    if isinstance(result, list):
        return f"{len(result)} item(s)"
    if isinstance(result, dict):
        return "ok"
    if isinstance(result, str):
        return f"{len(result)} chars" if len(result) > 40 else (result.splitlines()[0] if result else "ok")
    return "ok"


# ---------------------------------------------------------------------------
# HTTP glue — talks only to the packaged harness server.
# ---------------------------------------------------------------------------


async def _open_sessions(http: httpx.AsyncClient) -> list[str]:
    resp = await http.get("/api/sessions")
    resp.raise_for_status()
    return [
        session["workflow_id"]
        for session in resp.json()
        if session.get("agent_workflow_type") in CHRONICLER_WORKFLOW_TYPES
        and not session.get("closed", False)
    ]


async def _pending_callbacks(http: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    resp = await http.get(f"/api/status/{session_id}")
    resp.raise_for_status()
    return resp.json().get("pending_callbacks", [])


async def _post_callback_result(
    http: httpx.AsyncClient, session_id: str, tool_id: str, result: Any, error: str | None
) -> bool:
    body: dict[str, Any] = {"session_id": session_id, "tool_id": tool_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    resp = await http.post("/api/callback-result", json=body)
    if resp.status_code == 200:
        return True
    # 409 = already resolved / unknown id (another bridge won the race, or a double-post across
    # polls). Treat as settled so we stop retrying; anything else is worth logging.
    if resp.status_code != 409:
        print(f"  ! callback-result {resp.status_code}: {resp.text}")
    return resp.status_code == 409


async def _poll_once(http: httpx.AsyncClient, root: Path, resolved: set[str]) -> None:
    try:
        session_ids = await _open_sessions(http)
    except httpx.HTTPError:
        return  # server not up yet / transient — try again next tick
    for session_id in session_ids:
        try:
            pending = await _pending_callbacks(http, session_id)
        except httpx.HTTPError:
            continue
        for call in pending:
            tool_id = call.get("tool_id")
            if not tool_id or tool_id in resolved:
                continue
            tool_name = call.get("tool_name", "?")
            tool_input = call.get("tool_input", {})
            result, error = await execute_callback(root, tool_name, tool_input)
            summary = error if error is not None else _result_summary(result)
            print(f"  · {tool_name}({_summarize_args(tool_input)}) → {summary}")
            if await _post_callback_result(http, session_id, tool_id, result, error):
                resolved.add(tool_id)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://localhost:8000", help="Harness server URL.")
    parser.add_argument(
        "--dir",
        default="./chronicler-local",
        help="Local root: recordings/, sessions.json, transcripts/, site/ (created if missing).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between polls for pending callbacks.",
    )
    args = parser.parse_args()

    root = Path(args.dir).expanduser()
    _recordings_dir(root).mkdir(parents=True, exist_ok=True)

    print(f"Chronicler local bridge → serving {root.resolve()}")
    print(f"Fulfilling filesystem + session callbacks from {args.server} (chat stays in the web UI).")
    if not os.environ.get("GEMINI_API_KEY"):
        print("  ! GEMINI_API_KEY not set — upload_recording (transcription) will fail until it is.")

    resolved: set[str] = set()
    async with httpx.AsyncClient(base_url=args.server, timeout=60.0) as http:
        while True:
            await _poll_once(http, root, resolved)
            await asyncio.sleep(args.poll_interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
