"""Durable activity-backed tools for the Chronicler agent — real Gemini audio work, NO local disk.

Each tool is an ``@agent.activity_tool_defn``: the decorated name is the in-workflow dispatcher
the workflow calls (via ``run_tool``); ``agent.tool_activity(tool)`` returns the activity body the
worker registers (see ``ALL_ACTIVITIES``). Because they run as Temporal activities on the worker —
outside the workflow sandbox, no determinism constraints — they call the ``google.genai`` SDK
directly. The workflow never touches Gemini for audio; it just orchestrates these durable calls
(via a Code Mode script the model writes, or the SessionScribe subagent).

**Stateless worker.** These activities write NOTHING to the worker's filesystem — imagine the
worker as an ephemeral k8s pod. All durable state (recordings, the session registry, transcripts,
synthesized audio, the static site) lives on the USER's machine and is reached through callback
tools fulfilled by the local bridge (see ``local_fs_tools.py`` / ``local_bridge.py``). So:

  * **Recordings** are uploaded to the Gemini Files API by the bridge; ``transcribe_recording``
    takes that :class:`GeminiFileRef` and transcribes from it — raw audio never reaches the worker.
  * **Synthesized audio** is returned as bytes (base64 WAV); the agent writes it onto the user's
    machine via a callback. The worker does not save a file.
  * **Transcripts** are persisted on the user's machine (``save_transcript`` callback). A transient
    in-process cache (:data:`_TRANSCRIPTS`) is kept only as a same-worker optimization so
    ``summarize``/``extract``/``answer`` can address a just-produced transcript by id; it is memory,
    not disk, and the user's machine remains the source of truth. Those tools also accept the
    transcript text directly, so a cold worker can be fed the transcript re-read from the user's disk.

The same pydantic models are the Gemini response schema AND the tool return type. Transcription
heartbeats — it's the long job.

No ``from __future__ import annotations`` — these modules cross Temporal's pydantic converter and
stringized annotations trip its TypeAdapter build.
"""

import asyncio
import base64
import io
import os
import uuid
import wave
from datetime import timedelta
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
    GeminiFileRef,
    NotificationResult,
    NotifyRequest,
    SampleRecording,
    SessionSummary,
    SynthesizeRequest,
    Transcript,
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

# Full transcripts, cached by session_id so summarize/extract/answer can address a just-produced
# transcript by id without re-sending the text. In-PROCESS only (memory, never disk) — a same-worker
# optimization; the user's machine is the durable source of truth (see the module docstring).
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


async def _run_with_heartbeat(coro: Awaitable[T], *, every: float = 15.0) -> T:
    """Await ``coro`` while emitting a Temporal heartbeat every ``every`` seconds, so a long
    transcription is visibly alive (and cancellation/timeout behave correctly)."""
    task = asyncio.ensure_future(coro)
    while True:
        done, _ = await asyncio.wait({task}, timeout=every)
        if task in done:
            return task.result()
        activity.heartbeat("working")


def _require_transcript(session_id: str, transcript_text: str | None) -> str:
    """Resolve the transcript text to work from: the caller-supplied text (from a transcript the
    agent re-read off the user's machine) if given, else the same-worker cache. Raises a clear,
    non-retryable error if neither is available (cold worker, no text passed)."""
    if transcript_text is not None:
        return transcript_text
    cached = _TRANSCRIPTS.get(session_id)
    if cached is None:
        raise ApplicationError(
            f"no transcript for {session_id!r} on this worker — transcribe it first, or pass "
            f"transcript_text (read it from the user's machine with read_transcript).",
            type="NotTranscribed",
            non_retryable=True,
        )
    return cached.full_text


def _pcm_to_wav_bytes(pcm: bytes, *, rate: int = 24000) -> tuple[bytes, float]:
    """Wrap raw 16-bit mono PCM (Gemini TTS output) in a WAV container IN MEMORY. Returns
    ``(wav_bytes, duration_seconds)`` — no file is written (the worker has no disk)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buffer.getvalue(), len(pcm) / (rate * 2)


# A CONTINUING campaign arc, in play order — each scene picks up the previous one's cliffhanger,
# with a recurring party (Aurelius), NPC (Borne Ironhand), and threads (the Duskblade cult, the
# black bell, the fear-compass). generate_sample_audio indexes into this by installment, so N
# samples come out as installments 1..N — a story that continues, never a random duplicate. Two
# voices; speaker tags "DM"/"Player" match the multi-speaker TTS config below. Kept brief so each
# sample is cheap to synthesize (and transcribe).
_SAMPLE_SCENES: list[dict[str, str]] = [
    {
        "title": "The Whispering Crypt",
        "script": (
            'DM: The tavern door groans open. A one-eyed dwarf behind the bar eyes your muddy '
            'boots. "You\'re the ones asking about the Whispering Crypt," he grunts.\n'
            'Player: Aurelius slides a gold piece across the bar. "Depends who\'s asking. What do '
            'you know about it, Borne?"\n'
            'DM: Borne pockets the coin. "The Duskblade cult took it a fortnight past. Take the '
            'goat path, not the road — and whatever you do, don\'t ring the black bell."\n'
            'Player: "Naturally, now I want to ring the black bell. But fine — the goat path it '
            'is."'
        ),
    },
    {
        "title": "The Sunken Market",
        "script": (
            'DM: The goat path drops you into a flooded market, stalls half-drowned in black '
            'water. A hooded merchant beckons. "Fresh from the deep — a compass that points to '
            'what you fear most."\n'
            'Player: Aurelius eyes it. "That is the worst possible product. How much?"\n'
            'DM: "One memory. Your choice which." She smiles too widely. "The cult buys them by '
            'the crateful, you know. For the bell."\n'
            'Player: I check the exits, filing that away. "Hold it for me. We have a crypt to '
            'reach first."'
        ),
    },
    {
        "title": "The Black Bell",
        "script": (
            'DM: The goat path ends at the crypt. Below the altar hangs the black bell, and '
            'around it Duskblade cultists chant over a ring of stolen memories.\n'
            'Player: Aurelius\'s hand drifts to the bell-rope. "Borne said don\'t. So naturally." '
            'Then I stop. "No. We cut it down instead."\n'
            'DM: Your blade parts the rope. The bell drops — silent — into the dark, and the '
            'chanting chokes off. Every hooded head turns toward you at once.\n'
            'Player: "Well. That got their attention. Weapons out."'
        ),
    },
    {
        "title": "The Ashen Council",
        "script": (
            'DM: They don\'t fight — they parley. Five masked elders sit around a ring of cold '
            'ash. The eldest speaks: "You cut down our bell. That bell was our banner once."\n'
            'Player: Aurelius stays standing. "It was ringing over villages you let starve. What '
            'are you really digging for down here?"\n'
            'DM: A long silence. "The thing the bell was keeping asleep. Sit — if you\'d fight it, '
            'you\'ll need the compass you left with the merchant."\n'
            'Player: "Of course we will." I keep one hand near my blade. "Start talking."'
        ),
    },
    {
        "title": "The Memory's Price",
        "script": (
            'DM: Back at the sunken market, the merchant already has the compass waiting. "To '
            'find what sleeps under the crypt, it must point inward. The price stands. One '
            'memory."\n'
            'Player: Aurelius turns the compass over. "Then take the day I earned this sword. I '
            'remember what it cost. I don\'t need to remember the ceremony."\n'
            'DM: The needle shivers, then swings — not to the crypt, but back toward Borne\'s '
            'tavern. The merchant\'s smile finally falters.\n'
            'Player: "...The compass thinks our friendly dwarf is what I fear most. Let\'s go ask '
            'him why."'
        ),
    },
]


def _synthesize_sample_pcm(script: str) -> bytes:
    """Synthesize a two-voice (DM + Player) sample recording; returns raw PCM. Worker-side helper
    for :func:`generate_sample_audio_activity` — mirrors ``seed_session.py`` but as a tool."""
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
    return resp.candidates[0].content.parts[0].inline_data.data


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
# Tools — pure Gemini compute, no filesystem
# ---------------------------------------------------------------------------


@agent.activity_tool_defn(
    name="generate_sample_audio",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def generate_sample_audio_activity(installment: int = 1) -> SampleRecording:
    """Synthesize a short SYNTHETIC sample session recording (two voices, via Gemini TTS) and
    return it as bytes — so a fresh setup has something to transcribe/process without a real
    recording. `installment` is the 1-based position in a CONTINUING story: installment 1 is the
    opening scene, 2 continues it, and so on, so successive samples form one campaign rather than
    repeating. To create several at once, pass consecutive installments (1, 2, 3, …) — never the
    same one twice. Returns the WAV as base64 plus a title and the script; SAVE it onto the user's
    machine with `save_recording` to register it. Use this when there are no sessions yet."""
    scene = _SAMPLE_SCENES[(max(1, installment) - 1) % len(_SAMPLE_SCENES)]
    pcm = await asyncio.to_thread(_synthesize_sample_pcm, scene["script"])
    wav_bytes, duration = _pcm_to_wav_bytes(pcm)
    activity.logger.info(
        "generate_sample_audio: %.1fs of %r (%d bytes)", duration, scene["title"], len(wav_bytes)
    )
    return SampleRecording(
        title=scene["title"],
        audio_base64=base64.b64encode(wav_bytes).decode("ascii"),
        script=scene["script"],
    )


@agent.activity_tool_defn(
    name="transcribe_recording",
    activity_config=ActivityConfig(
        start_to_close_timeout=_TRANSCRIBE_TIMEOUT,
        heartbeat_timeout=timedelta(seconds=60),
    ),
)
async def transcribe_recording_activity(
    file_ref: GeminiFileRef, session_id: str
) -> Transcript:
    """Transcribe a session's audio with Gemini (speaker labels + timestamps) and return the FULL
    transcript. `file_ref` is a Gemini upload the local bridge produced from the user's recording
    (get it first with `upload_recording`) — the worker never holds the audio. This is the
    long-running step; it heartbeats while Gemini works. Persist the result on the user's machine
    with `save_transcript`; summarize/extract can then address it by session_id this same run."""
    client = _client()
    uploaded = await client.aio.files.get(name=file_ref.name)
    activity.logger.info("Transcribing %s from %s", session_id, file_ref.name)

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
    _TRANSCRIPTS[session_id] = transcript  # same-worker cache; persist to the user's disk yourself
    return transcript


@agent.activity_tool_defn(
    name="summarize_transcript",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def summarize_transcript_activity(
    session_id: str, transcript_text: str | None = None
) -> SessionSummary:
    """Summarize an already-transcribed session into a TL;DR, key beats, and the cliffhanger.
    Addresses the transcript by `session_id` (from a transcription done this run); if the worker
    doesn't have it, read it from the user's machine and pass its `full_text` as `transcript_text`."""
    full_text = _require_transcript(session_id, transcript_text)
    client = _client()
    resp = await client.aio.models.generate_content(
        model=SUMMARY_MODEL,
        contents=[
            "Summarize this D&D session transcript. Give a punchy TL;DR (2-3 sentences), the "
            "key story beats as short bullets, and the cliffhanger / where it left off (or null "
            "if none).\n\n" + full_text
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
async def extract_entities_activity(
    session_id: str, transcript_text: str | None = None
) -> CampaignEntities:
    """Extract the notable entities from a transcribed session — NPCs, player characters,
    locations, items, factions, and quests — with a short description and any aliases. Addresses
    the transcript by `session_id`; pass `transcript_text` if this worker doesn't have it cached."""
    full_text = _require_transcript(session_id, transcript_text)
    client = _client()
    resp = await client.aio.models.generate_content(
        model=SUMMARY_MODEL,
        contents=[
            "Extract notable entities from this D&D session transcript. For each: name, kind "
            "(one of npc, pc, location, item, faction, quest), any aliases, and a one-line "
            "description.\n\n" + full_text
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
async def answer_question_activity(
    session_id: str, question: str, transcript_text: str | None = None
) -> str:
    """Answer a question about a single session, grounded in its transcript. Addresses the
    transcript by `session_id`; pass `transcript_text` if this worker doesn't have it cached.
    Used by the SessionScribe subagent for per-session Q&A."""
    full_text = _require_transcript(session_id, transcript_text)
    client = _client()
    resp = await client.aio.models.generate_content(
        model=SUMMARY_MODEL,
        contents=[
            "Answer the question using ONLY this D&D session transcript. If the transcript "
            "doesn't cover it, say so plainly.\n\nQuestion: "
            + question
            + "\n\nTranscript:\n"
            + full_text
        ],
    )
    return resp.text or ""


@agent.activity_tool_defn(
    name="synthesize_audio",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
)
async def synthesize_audio_activity(request: SynthesizeRequest) -> AudioArtifact:
    """Synthesize narrated audio from text via Gemini TTS (e.g. a 'previously on' recap or a
    session intro) and return it as bytes (base64 WAV). The worker saves nothing — write the bytes
    onto the user's machine (e.g. into the static site under site/) with `write_binary_file`."""
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
    wav_bytes, duration_s = _pcm_to_wav_bytes(pcm)
    activity.logger.info("Synthesized %s audio (%.1fs, %d bytes)", request.kind, duration_s, len(wav_bytes))
    return AudioArtifact(
        artifact_id=uuid.uuid4().hex[:12],
        kind=request.kind,
        voice=request.voice,
        script_text=request.script_text,
        audio_base64=base64.b64encode(wav_bytes).decode("ascii"),
        duration_s=duration_s,
    )


# A self-contained "old tavern" stylesheet for the generated campaign site. No external fonts or
# assets (system serif stacks + CSS-drawn parchment/wood), so the site opens straight off disk with
# no server or internet. The HTML the agent writes targets the documented class contract below.
_TAVERN_CSS = """\
/* Chronicler — "old tavern" theme. Self-contained: no external fonts/assets.
   HTML contract (classes the pages should use):
     header.tavern-sign  > h1 (tavern/site name) + p.campaign
     main.ledger         > article.session-card (one per session)
     .session-card       > h2 (title) + p.session-meta + p.tl-dr + ul.beats
                           + p.cliffhanger + section.bard-recap (holds <audio>)
                           + ul.entities > li.entity-chip[data-kind=npc|pc|location|item|faction|quest]
     footer.colophon
   On index.html link this as "assets/tavern.css"; from site/sessions/*.html use "../assets/tavern.css". */

:root {
  --parchment: #efe0bd;
  --parchment-deep: #e3cf9f;
  --ink: #3a2a17;
  --ink-soft: #6b5334;
  --wood: #3a2413;
  --wood-light: #5a3a20;
  --gold: #b4842b;
  --wine: #7d2b25;
  --shadow: rgba(38, 22, 8, 0.35);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 2rem 1rem 4rem;
  color: var(--ink);
  font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  line-height: 1.6;
  background-color: var(--parchment);
  background-image:
    radial-gradient(circle at 20% 15%, rgba(255, 250, 235, 0.6), transparent 45%),
    radial-gradient(circle at 85% 80%, rgba(120, 80, 30, 0.18), transparent 55%),
    repeating-linear-gradient(90deg, rgba(120, 85, 40, 0.04) 0 2px, transparent 2px 4px);
}

a { color: var(--wine); }

.tavern-sign {
  max-width: 820px;
  margin: 0 auto 2.5rem;
  padding: 1.6rem 1rem 1.9rem;
  text-align: center;
  color: var(--parchment);
  background:
    linear-gradient(180deg, var(--wood-light), var(--wood));
  border: 3px solid var(--gold);
  border-radius: 10px;
  box-shadow: 0 10px 24px var(--shadow), inset 0 0 0 6px rgba(0, 0, 0, 0.15);
}

.tavern-sign h1 {
  margin: 0;
  font-size: clamp(2rem, 5vw, 3rem);
  letter-spacing: 0.06em;
  font-variant: small-caps;
  color: var(--gold);
  text-shadow: 0 2px 2px rgba(0, 0, 0, 0.5);
}

.tavern-sign h1::before,
.tavern-sign h1::after { content: "\\2766"; color: var(--parchment); margin: 0 0.5em; opacity: 0.7; }

.tavern-sign .campaign { margin: 0.3rem 0 0; font-style: italic; opacity: 0.9; }

.ledger { max-width: 820px; margin: 0 auto; }

.session-card {
  position: relative;
  margin: 0 0 2rem;
  padding: 1.6rem 1.8rem;
  background: linear-gradient(180deg, #f6ead0, var(--parchment-deep));
  border: 1px solid var(--wood-light);
  border-left: 6px solid var(--gold);
  border-radius: 6px;
  box-shadow: 0 6px 16px var(--shadow);
}

.session-card h2 {
  margin: 0 0 0.2rem;
  color: var(--wine);
  font-size: 1.6rem;
  border-bottom: 3px double var(--gold);
  padding-bottom: 0.4rem;
}

.session-meta { margin: 0.2rem 0 1rem; font-style: italic; color: var(--ink-soft); font-size: 0.9rem; }
.tl-dr { font-size: 1.08rem; }

ul.beats { list-style: none; padding-left: 1.4rem; }
ul.beats li { position: relative; margin: 0.25rem 0; }
ul.beats li::before { content: "\\2694"; position: absolute; left: -1.4rem; color: var(--gold); }

.cliffhanger {
  margin-top: 1rem;
  padding: 0.7rem 1rem;
  font-style: italic;
  background: rgba(125, 43, 37, 0.08);
  border-left: 4px solid var(--wine);
  border-radius: 0 4px 4px 0;
}

.bard-recap {
  margin-top: 1.2rem;
  padding: 1rem 1.1rem 1.1rem;
  background: var(--wood);
  color: var(--parchment);
  border-radius: 6px;
  border: 2px solid var(--gold);
}
.bard-recap h3 { margin: 0 0 0.6rem; font-variant: small-caps; letter-spacing: 0.05em; color: var(--gold); }
.bard-recap h3::before { content: "\\266B"; margin-right: 0.5em; }
.bard-recap audio { width: 100%; }

ul.entities { list-style: none; padding: 0; margin: 1.1rem 0 0; display: flex; flex-wrap: wrap; gap: 0.5rem; }
.entity-chip {
  padding: 0.2rem 0.7rem;
  font-size: 0.85rem;
  border-radius: 999px;
  border: 1px solid var(--wood-light);
  background: rgba(180, 132, 43, 0.16);
}
.entity-chip[data-kind="npc"]      { background: rgba(125, 43, 37, 0.16); }
.entity-chip[data-kind="pc"]       { background: rgba(60, 100, 60, 0.18); }
.entity-chip[data-kind="location"] { background: rgba(60, 90, 130, 0.16); }
.entity-chip[data-kind="item"]     { background: rgba(180, 132, 43, 0.22); }
.entity-chip[data-kind="faction"]  { background: rgba(90, 58, 32, 0.20); }
.entity-chip[data-kind="quest"]    { background: rgba(110, 70, 130, 0.16); }

.colophon { max-width: 820px; margin: 2.5rem auto 0; text-align: center; font-style: italic; color: var(--ink-soft); }
.colophon::before { content: "\\2766"; color: var(--gold); margin-right: 0.5em; }
"""


@agent.activity_tool_defn(
    name="tavern_theme",
    activity_config=ActivityConfig(start_to_close_timeout=_QUICK_TIMEOUT),
    inherently_safe=True,
)
async def tavern_theme_activity() -> str:
    """Return a self-contained 'old tavern' CSS stylesheet for the campaign site (aged parchment,
    dark wood, candlelight gold, wine-red — system fonts only, so it opens straight off disk).
    Write it to `site/assets/tavern.css` with `write_file`, link it from every page, and build the
    HTML to the class contract in the stylesheet's top comment (header.tavern-sign, article.
    session-card with .tl-dr / ul.beats / .cliffhanger / section.bard-recap / ul.entities, etc.).
    Use this instead of hand-writing CSS so the whole site looks consistent."""
    return _TAVERN_CSS


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
# in-workflow dispatchers the workflow calls via run_tool). The session registry, recordings,
# transcripts, and the static site are NOT here — they live on the user's machine, reached via the
# callback tools in local_fs_tools.py.
ALL_ACTIVITIES = [
    agent.tool_activity(t)
    for t in (
        generate_sample_audio_activity,
        transcribe_recording_activity,
        summarize_transcript_activity,
        extract_entities_activity,
        answer_question_activity,
        synthesize_audio_activity,
        tavern_theme_activity,
        notify_activity,
    )
]
