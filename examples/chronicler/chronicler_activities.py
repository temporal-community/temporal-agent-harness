"""Durable activity-backed tools for the Chronicler agent — real Gemini audio work.

Each tool is an ``@agent.activity_tool_defn``: the decorated name is the in-workflow dispatcher
the workflow calls (via ``run_tool``); ``agent.tool_activity(tool)`` returns the activity body the
worker registers (see ``ALL_ACTIVITIES``). Because they run as Temporal activities on the worker
— outside the workflow sandbox, with no determinism constraints — they call the ``google.genai``
SDK directly. The workflow never touches Gemini for audio; it just orchestrates these durable
calls (via a Code Mode script the model writes).

Design choices that matter:
  * **Address heavy data by id, not by value.** ``transcribe_session`` caches the full transcript
    worker-side and returns a lightweight :class:`TranscriptMeta`; ``summarize_transcript`` /
    ``extract_entities`` read the cached transcript by ``session_id``. So a multi-hour transcript
    never round-trips through the model's context or a tool-result string.
  * **The same pydantic models are the Gemini response schema AND the tool return type.** Gemini
    structured output is asked to fill these models, and the tool returns them — one typed shape
    end to end.
  * **Transcription heartbeats.** It's the long job; it heartbeats while the model call is in
    flight so a multi-minute transcription isn't mistaken for a stalled activity.

No ``from __future__ import annotations`` — these modules cross Temporal's pydantic converter and
stringized annotations trip its TypeAdapter build.
"""

import asyncio
import json
import os
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent

from .chronicler_models import (
    AudioArtifact,
    CampaignEntities,
    Entity,
    IngestResult,
    NotificationResult,
    NotifyRequest,
    SessionList,
    SessionRef,
    SessionSummary,
    SynthesizeRequest,
    Transcript,
    TranscriptMeta,
    TranscriptSegment,
)
from .notifier import get_notifier

# --- Models (overridable per deployment; audio model availability varies by API tier) --------
TRANSCRIBE_MODEL = os.environ.get("CHRONICLER_TRANSCRIBE_MODEL", "gemini-3.1-flash-lite")
SUMMARY_MODEL = os.environ.get("CHRONICLER_SUMMARY_MODEL", "gemini-3.1-flash-lite")
# TTS uses a dedicated speech model; default to the canonical preview name, overridable since
# the exact string depends on your API access.
TTS_MODEL = os.environ.get("CHRONICLER_TTS_MODEL", "gemini-2.5-flash-preview-tts")

# --- Timeouts. Transcription is the long one; the rest are quick model calls. -----------------
_TRANSCRIBE_TIMEOUT = timedelta(minutes=20)
_QUICK_TIMEOUT = timedelta(minutes=3)

# --- On-disk archive + worker-side caches -----------------------------------------------------
SESSIONS_DIR = Path(__file__).parent / "sessions"
REGISTRY_PATH = SESSIONS_DIR / "sessions.json"
ARTIFACTS_DIR = SESSIONS_DIR / "artifacts"

# Full transcripts, cached by session_id so heavy text stays worker-side and is addressed by id.
# In-process for the demo's lifetime — fine for a single-worker prototype (mirrors Monty's store).
_TRANSCRIPTS: dict[str, Transcript] = {}

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _client() -> genai.Client:
    """A genai client with the worker's GEMINI_API_KEY passed explicitly (matching worker.py) —
    the SDK doesn't reliably auto-read GEMINI_API_KEY. The worker requires the key to boot."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ApplicationError(
            "GEMINI_API_KEY is not set on the worker", type="MissingApiKey", non_retryable=True
        )
    return genai.Client(api_key=api_key)


# Audio containers Gemini can transcribe. Used to discover recordings in the sessions/ dir.
_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".aiff", ".aif"}


def _load_records() -> list[dict]:
    """Read the raw ordered list of session records from the registry (empty if none)."""
    if not REGISTRY_PATH.exists():
        return []
    return json.loads(REGISTRY_PATH.read_text()).get("sessions", [])


def _write_records(records: list[dict]) -> None:
    """Write the registry atomically (temp file + rename) so a concurrent read can't see a
    half-written file."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"sessions": records}, indent=2) + "\n")
    os.replace(tmp, REGISTRY_PATH)


def _load_registry() -> dict[str, dict]:
    """Read the session archive registry (session_id -> record). Empty if none seeded yet."""
    return {rec["session_id"]: rec for rec in _load_records()}


def _prettify(stem: str) -> str:
    """Turn a filename stem into a human title, e.g. 'the_black_bell-02' -> 'The Black Bell 02'."""
    return " ".join(w for w in stem.replace("-", " ").replace("_", " ").split()).title()


def _session_ref(rec: dict) -> SessionRef:
    return SessionRef(
        session_id=rec["session_id"],
        campaign_id=rec["campaign_id"],
        title=rec["title"],
        recorded_at=rec["recorded_at"],
        number=rec["number"],
        transcribed=rec["session_id"] in _TRANSCRIPTS,
    )


async def _run_with_heartbeat(coro: Awaitable[T], *, every: float = 15.0) -> T:
    """Await ``coro`` while emitting a Temporal heartbeat every ``every`` seconds, so a long
    transcription is visibly alive (and cancellation/timeout behave correctly)."""
    task = asyncio.ensure_future(coro)
    while True:
        done, _ = await asyncio.wait({task}, timeout=every)
        if task in done:
            return task.result()
        activity.heartbeat("working")


def _require_transcript(session_id: str) -> Transcript:
    transcript = _TRANSCRIPTS.get(session_id)
    if transcript is None:
        raise ApplicationError(
            f"session {session_id!r} has not been transcribed yet — call "
            f"transcribe_session({session_id!r}) first.",
            type="NotTranscribed",
            non_retryable=True,
        )
    return transcript


def _pcm_to_wav(pcm: bytes, path: Path, *, rate: int = 24000) -> float:
    """Wrap raw 16-bit mono PCM (Gemini TTS output) in a WAV container. Returns duration (s)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return len(pcm) / (rate * 2)


# Short two-voice scenes used by generate_sample_session. Speaker tags "DM"/"Player" match the
# multi-speaker TTS config below. Kept brief so a sample is cheap to synthesize (and transcribe).
_SAMPLE_SCENES: list[dict[str, str]] = [
    {
        "title": "The Whispering Crypt",
        "script": (
            'DM: The tavern door groans open. A one-eyed dwarf behind the bar eyes your muddy '
            'boots. "You\'re the ones asking about the Whispering Crypt," he grunts.\n'
            'Player: I slide a gold piece across the bar. "Depends who\'s asking. What do you '
            'know about it?"\n'
            'DM: He pockets the coin. "The Duskblade cult took it a fortnight past. Folk who go '
            'up the hill don\'t come back. Take the goat path, not the road — and whatever you '
            'do, don\'t ring the black bell."\n'
            'Player: "Naturally, now I want to ring the black bell. But fine — the goat path it '
            'is."'
        ),
    },
    {
        "title": "The Sunken Market",
        "script": (
            'DM: Lantern light wavers over stalls half-drowned in black water. A hooded merchant '
            'beckons. "Fresh from the deep — a compass that points to what you fear most."\n'
            'Player: "That is the worst possible product. How much?"\n'
            'DM: "One memory. Your choice which." She smiles too widely.\n'
            'Player: I check the exits before I answer. "Give me a moment to browse."'
        ),
    },
    {
        "title": "The Ashen Council",
        "script": (
            'DM: Five masked figures sit around a ring of cold ash. The eldest speaks: "You '
            'burned the Duskblade banner. That was our banner once."\n'
            'Player: "It was flying over a village you\'d have let starve."\n'
            'DM: A long silence. Then: "Sit. If you would fight them properly, you\'ll need to '
            'know what they buried under the crypt."\n'
            'Player: I sit, but I keep one hand near my blade. "Start talking."'
        ),
    },
]


def _synthesize_session_wav(script: str, out_path: Path) -> float:
    """Synthesize a two-voice (DM + Player) sample recording to ``out_path``; returns duration.

    Worker-side helper for :func:`generate_sample_session_activity` — mirrors the standalone
    ``seed_session.py`` script, but as a durable-tool building block."""
    client = _client()
    resp = client.models.generate_content(
        model=TTS_MODEL,
        contents=script,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
                    speaker_voice_configs=[
                        types.SpeakerVoiceConfig(
                            speaker="DM",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name="Charon"
                                )
                            ),
                        ),
                        types.SpeakerVoiceConfig(
                            speaker="Player",
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                    voice_name="Puck"
                                )
                            ),
                        ),
                    ]
                )
            ),
        ),
    )
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    return _pcm_to_wav(pcm, out_path)


# Internal response-schema drafts: the shapes Gemini fills. We add the session_id ourselves so
# the model never has to echo it. Kept separate from the public models for exactly that reason.
class _TranscriptDraft(BaseModel):
    duration_s: float
    segments: list[TranscriptSegment]


class _SummaryDraft(BaseModel):
    tl_dr: str
    beats: list[str]
    cliffhanger: str | None


class _EntitiesDraft(BaseModel):
    entities: list[Entity]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@agent.activity_tool_defn(
    name="generate_sample_session",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def generate_sample_session_activity(
    campaign_id: str | None = None, title: str | None = None
) -> SessionRef:
    """Generate a short SYNTHETIC sample session recording (two voices, via Gemini TTS) and
    register it — so a fresh project has something to transcribe/process without a real recording.
    Use this when there are no sessions yet and the user wants to try the pipeline. `campaign_id`
    defaults to 'duskblade'; each call produces a different scene. Returns the new session."""
    records = _load_records()
    number = max((r["number"] for r in records), default=0) + 1
    scene = _SAMPLE_SCENES[(number - 1) % len(_SAMPLE_SCENES)]
    session_id = f"sample-{number:02d}"
    while any(r["session_id"] == session_id for r in records):
        number += 1
        session_id = f"sample-{number:02d}"

    out_path = SESSIONS_DIR / f"{session_id}.wav"
    duration = await asyncio.to_thread(_synthesize_session_wav, scene["script"], out_path)
    activity.logger.info(
        "generate_sample_session: wrote %s (%.1fs) — %r", out_path.name, duration, scene["title"]
    )

    record = {
        "session_id": session_id,
        "campaign_id": campaign_id or "duskblade",
        "title": title or scene["title"],
        "recorded_at": datetime.now().date().isoformat(),
        "number": number,
        "audio_file": out_path.name,
    }
    records.append(record)
    _write_records(records)
    return _session_ref(record)


@agent.activity_tool_defn(
    name="ingest_sessions",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def ingest_sessions_activity(campaign_id: str) -> IngestResult:
    """Scan the sessions/ directory for audio recordings not yet in the registry, register any
    new ones under the given campaign (auto-assigning id, play-order number, and title from the
    filename), and return what was added plus the campaign's full session list. Call this to pick
    up recordings the user just dropped in — no manual registry editing needed."""
    records = _load_records()
    known_files = {rec["audio_file"] for rec in records}
    max_number = max((rec["number"] for rec in records), default=0)
    existing_ids = {rec["session_id"] for rec in records}

    # Discover audio files directly in sessions/ (skip the artifacts/ output dir and non-audio).
    found = sorted(
        p for p in SESSIONS_DIR.iterdir() if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    )
    added: list[str] = []
    for path in found:
        if path.name in known_files:
            continue
        session_id = path.stem
        while session_id in existing_ids:  # keep ids unique if two stems collide
            session_id = f"{session_id}-x"
        existing_ids.add(session_id)
        max_number += 1
        recorded_at = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        records.append(
            {
                "session_id": session_id,
                "campaign_id": campaign_id,
                "title": _prettify(path.stem),
                "recorded_at": recorded_at,
                "number": max_number,
                "audio_file": path.name,
            }
        )
        added.append(session_id)

    if added:
        _write_records(records)
    activity.logger.info("ingest_sessions: added %d new recording(s): %s", len(added), added)

    campaign = [_session_ref(rec) for rec in records if rec["campaign_id"] == campaign_id]
    campaign.sort(key=lambda r: r.number)
    return IngestResult(
        added=added, already_registered=len(known_files), sessions=campaign
    )


@agent.activity_tool_defn(
    name="list_sessions",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def list_sessions_activity(campaign_id: str | None = None) -> SessionList:
    """List recorded sessions in play order. Pass a `campaign_id` to filter to one campaign, or
    pass `null` to list ALL sessions across every campaign — each SessionRef includes its
    `campaign_id`, so passing null is how you discover what campaigns exist (don't guess a
    campaign name). `transcribed` tells you which already have a transcript cached. Also reports
    `unregistered_files`: audio in the archive not registered yet — offer to ingest them."""
    records = _load_records()
    refs = [
        _session_ref(rec)
        for rec in sorted(records, key=lambda r: r["number"])
        if not campaign_id or rec["campaign_id"] == campaign_id
    ]
    known_files = {rec["audio_file"] for rec in records}
    unregistered = sorted(
        p.name
        for p in SESSIONS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS and p.name not in known_files
    )
    return SessionList(sessions=refs, unregistered_files=unregistered)


@agent.activity_tool_defn(
    name="transcribe_session",
    activity_config=ActivityConfig(
        start_to_close_timeout=_TRANSCRIBE_TIMEOUT,
        heartbeat_timeout=timedelta(seconds=60),
    ),
)
async def transcribe_session_activity(session_id: str) -> TranscriptMeta:
    """Transcribe a session's audio with Gemini (speaker labels + timestamps) and CACHE the full
    transcript. Returns lightweight metadata (duration, speakers, a preview) — fetch the full
    text with get_transcript only if you need it. This is the long-running step; it heartbeats
    while Gemini works."""
    registry = _load_registry()
    rec = registry.get(session_id)
    if rec is None:
        raise ApplicationError(
            f"unknown session_id {session_id!r}", type="UnknownSession", non_retryable=True
        )
    audio_path = SESSIONS_DIR / rec["audio_file"]
    if not audio_path.exists():
        raise ApplicationError(
            f"audio file missing for {session_id!r}: {audio_path}",
            type="MissingAudio",
            non_retryable=True,
        )

    activity.logger.info("Transcribing %s from %s", session_id, audio_path.name)
    client = _client()

    # Upload via the Files API (handles multi-hour sessions that exceed the inline request cap),
    # then wait for it to become ACTIVE.
    uploaded = await _run_with_heartbeat(client.aio.files.upload(file=str(audio_path)))
    while getattr(uploaded, "state", None) and str(uploaded.state) not in (
        "ACTIVE",
        "FileState.ACTIVE",
    ):
        if str(uploaded.state) in ("FAILED", "FileState.FAILED"):
            raise ApplicationError("Gemini file processing failed", type="FileProcessingFailed")
        await asyncio.sleep(2)
        activity.heartbeat("uploading")
        uploaded = await client.aio.files.get(name=uploaded.name)

    prompt = (
        "Transcribe this tabletop RPG (D&D) session audio verbatim. Produce diarized segments "
        "with a speaker label (DM, or a player/character name if identifiable), start and end "
        "times in seconds, and the spoken text. Return the full session."
    )
    try:
        resp = await _run_with_heartbeat(
            client.aio.models.generate_content(
                model=TRANSCRIBE_MODEL,
                contents=[prompt, uploaded],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=_TranscriptDraft,
                ),
            )
        )
        draft: _TranscriptDraft = resp.parsed
        segments = draft.segments
        duration_s = draft.duration_s
    except Exception as e:
        # Structured transcription failed — fall back to a plain-text transcript so the pipeline
        # still yields something usable rather than erroring the whole turn.
        activity.logger.warning("structured transcription failed (%s); using plain text", e)
        resp = await _run_with_heartbeat(
            client.aio.models.generate_content(
                model=TRANSCRIBE_MODEL,
                contents=["Transcribe this audio verbatim.", uploaded],
            )
        )
        text = resp.text or ""
        segments = [TranscriptSegment(speaker="unknown", start_s=0.0, end_s=0.0, text=text)]
        duration_s = 0.0

    full_text = "\n".join(f"{s.speaker}: {s.text}" for s in segments)
    transcript = Transcript(
        session_id=session_id,
        model=TRANSCRIBE_MODEL,
        duration_s=duration_s,
        full_text=full_text,
        segments=segments,
    )
    _TRANSCRIPTS[session_id] = transcript

    speakers = sorted({s.speaker for s in segments})
    word_count = sum(len(s.text.split()) for s in segments)
    return TranscriptMeta(
        session_id=session_id,
        model=TRANSCRIBE_MODEL,
        duration_s=duration_s,
        segment_count=len(segments),
        word_count=word_count,
        speakers=speakers,
        preview=full_text[:600],
    )


@agent.activity_tool_defn(
    name="get_transcript",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def get_transcript_activity(session_id: str) -> Transcript:
    """Fetch the full cached transcript for a session. Large — prefer working from
    transcribe_session's metadata / summaries, and only fetch this when you truly need the text."""
    return _require_transcript(session_id)


@agent.activity_tool_defn(
    name="summarize_transcript",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def summarize_transcript_activity(session_id: str) -> SessionSummary:
    """Summarize an already-transcribed session into a TL;DR, key beats, and the cliffhanger.
    Reads the cached transcript by id (no need to pass the text)."""
    transcript = _require_transcript(session_id)
    client = _client()
    resp = await client.aio.models.generate_content(
        model=SUMMARY_MODEL,
        contents=[
            "Summarize this D&D session transcript. Give a punchy TL;DR (2-3 sentences), the "
            "key story beats as short bullets, and the cliffhanger / where it left off (or null "
            "if none).\n\n" + transcript.full_text
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_SummaryDraft,
        ),
    )
    draft: _SummaryDraft = resp.parsed
    return SessionSummary(
        session_id=session_id, tl_dr=draft.tl_dr, beats=draft.beats, cliffhanger=draft.cliffhanger
    )


@agent.activity_tool_defn(
    name="extract_entities",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def extract_entities_activity(session_id: str) -> CampaignEntities:
    """Extract the notable entities from a transcribed session — NPCs, player characters,
    locations, items, factions, and quests — with a short description and any aliases."""
    transcript = _require_transcript(session_id)
    client = _client()
    resp = await client.aio.models.generate_content(
        model=SUMMARY_MODEL,
        contents=[
            "Extract notable entities from this D&D session transcript. For each: name, kind "
            "(one of npc, pc, location, item, faction, quest), any aliases, and a one-line "
            "description.\n\n" + transcript.full_text
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_EntitiesDraft,
        ),
    )
    draft: _EntitiesDraft = resp.parsed
    return CampaignEntities(session_id=session_id, entities=draft.entities)


@agent.activity_tool_defn(
    name="answer_question",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def answer_question_activity(session_id: str, question: str) -> str:
    """Answer a question about a single session, grounded in its cached transcript. Used by the
    SessionScribe subagent for per-session Q&A."""
    transcript = _require_transcript(session_id)
    client = _client()
    resp = await client.aio.models.generate_content(
        model=SUMMARY_MODEL,
        contents=[
            "Answer the question using ONLY this D&D session transcript. If the transcript "
            "doesn't cover it, say so plainly.\n\nQuestion: "
            + question
            + "\n\nTranscript:\n"
            + transcript.full_text
        ],
    )
    return resp.text or ""


@agent.activity_tool_defn(
    name="synthesize_audio",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def synthesize_audio_activity(request: SynthesizeRequest) -> AudioArtifact:
    """Synthesize narrated audio from text via Gemini TTS (e.g. a 'previously on' recap or a
    session intro). Writes a WAV to the artifacts dir and returns a reference to it."""
    client = _client()
    resp = await client.aio.models.generate_content(
        model=TTS_MODEL,
        contents=request.script_text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=request.voice)
                )
            ),
        ),
    )
    pcm = resp.candidates[0].content.parts[0].inline_data.data
    artifact_id = uuid.uuid4().hex[:12]
    out_path = ARTIFACTS_DIR / f"{request.kind}-{artifact_id}.wav"
    duration_s = _pcm_to_wav(pcm, out_path)
    activity.logger.info("Synthesized %s audio -> %s (%.1fs)", request.kind, out_path.name, duration_s)
    return AudioArtifact(
        artifact_id=artifact_id,
        kind=request.kind,
        voice=request.voice,
        script_text=request.script_text,
        audio_path=str(out_path),
        duration_s=duration_s,
    )


@agent.activity_tool_defn(
    name="notify",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def notify_activity(request: NotifyRequest) -> NotificationResult:
    """Send a notification (e.g. 'transcription ready'). Delivered via the configured channel —
    in-app by default (visible here as a tool event + echoed in the reply), or a real webhook if
    the deployment sets one. Call this after finishing a long job like a transcription."""
    return await get_notifier().notify(request)


# The durable activity bodies to register on the worker (the module-level names above are the
# in-workflow dispatchers the workflow calls via run_tool).
ALL_ACTIVITIES = [
    agent.tool_activity(t)
    for t in (
        generate_sample_session_activity,
        ingest_sessions_activity,
        list_sessions_activity,
        transcribe_session_activity,
        get_transcript_activity,
        summarize_transcript_activity,
        extract_entities_activity,
        answer_question_activity,
        synthesize_audio_activity,
        notify_activity,
    )
]
