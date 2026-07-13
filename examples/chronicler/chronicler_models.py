"""Typed domain models for the Chronicler D&D session agent.

Every value that crosses the Temporal activity boundary — and every host-function
argument/return the Code Mode sandbox sees — is one of these pydantic models. They ARE the
contract: :func:`agent.code_mode_tool` reflects over the tool signatures and these models to
generate the sandbox's typed host-function stubs, and the model-authored script is statically
type-checked against them before it runs. A wrong field name comes back as an error to fix, not
a bad result. That is the whole "typed inputs and outputs" tenet, made enforceable.

No ``from __future__ import annotations`` here: these cross Temporal's pydantic converter, and
stringized annotations on nested models trip its TypeAdapter build. Concrete annotations only,
and each nested model is defined before the model that references it.
"""

from typing import Literal

from pydantic import BaseModel

# The kinds of things worth tracking across a campaign. A Literal (not a bare str) so the
# generated Code Mode stubs advertise the exact allowed values to the model.
EntityKind = Literal["npc", "pc", "location", "item", "faction", "quest"]

# Voices offered by Gemini TTS that suit a table read; surfaced to the model as a Literal so it
# can only pick a real one. (Full Gemini voice list is larger; this is a curated subset.)
NarratorVoice = Literal["Charon", "Kore", "Puck", "Fenrir", "Aoede", "Enceladus"]

# What a synthesized clip is for — shapes the default framing in the audio.
ArtifactKind = Literal["recap", "intro", "custom"]


# ---------------------------------------------------------------------------
# Sessions (the archive the agent works over)
# ---------------------------------------------------------------------------


class SessionRef(BaseModel):
    """A recorded session in the archive. The audio lives on the USER's machine (the worker has no
    disk); the model addresses sessions by ``session_id`` and the local bridge resolves the file.
    ``transcribed`` reflects whether a transcript has been persisted on the user's machine."""

    session_id: str
    campaign_id: str
    title: str
    recorded_at: str
    number: int
    transcribed: bool


class SessionList(BaseModel):
    """Registered sessions for a campaign, plus any audio files sitting in the archive dir that
    aren't registered yet (so the agent can offer to ingest them)."""

    sessions: list[SessionRef]
    unregistered_files: list[str]


class IngestResult(BaseModel):
    """Result of scanning the user's local recordings for audio and updating the registry."""

    added: list[str]
    already_registered: int
    sessions: list[SessionRef]


# ---------------------------------------------------------------------------
# Audio transfer between the user's machine and the stateless worker
# ---------------------------------------------------------------------------
# The worker never touches disk. Recordings live on the user's machine; the local bridge uploads
# one to the Gemini Files API and hands back this reference, which the worker transcribes from —
# so raw audio bytes never travel through the worker.


class GeminiFileRef(BaseModel):
    """A handle to a recording the local bridge uploaded to the Gemini Files API. The worker
    transcribes from this (re-fetching by ``name``) without ever holding the audio bytes. The
    bridge waits for the file to reach ACTIVE before returning, so the worker can use it directly."""

    name: str
    uri: str
    mime_type: str


class SampleRecording(BaseModel):
    """A freshly synthesized sample recording, returned as bytes (base64 WAV) for the local bridge
    to save on the user's machine — the worker keeps nothing. ``title`` seeds the session title."""

    title: str
    audio_base64: str
    script: str


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------


class TranscriptSegment(BaseModel):
    speaker: str
    start_s: float
    end_s: float
    text: str


class Transcript(BaseModel):
    """The full transcript. Potentially large — it is persisted on the user's machine (via the
    ``save_transcript`` callback) and re-read with ``read_transcript`` when needed. Most
    orchestration works off :class:`TranscriptMeta` so the full text never round-trips through the
    model's context."""

    session_id: str
    model: str
    duration_s: float
    full_text: str
    segments: list[TranscriptSegment]


class TranscriptMeta(BaseModel):
    """Lightweight result of a transcription: enough to reason about, without the full text.

    ``transcribe_session`` returns this (and caches the full :class:`Transcript` worker-side),
    so a multi-hour transcript never lands in the model's context or the tool result string."""

    session_id: str
    model: str
    duration_s: float
    segment_count: int
    word_count: int
    speakers: list[str]
    preview: str


# ---------------------------------------------------------------------------
# Summarization + entity extraction
# ---------------------------------------------------------------------------


class SessionSummary(BaseModel):
    """A session boiled down to a TL;DR, its key story beats, and where it left off."""

    session_id: str
    tl_dr: str
    beats: list[str]
    cliffhanger: str | None


class Entity(BaseModel):
    """A notable thing in the campaign — an NPC, PC, location, item, faction, or quest."""

    name: str
    kind: EntityKind
    aliases: list[str]
    description: str


class CampaignEntities(BaseModel):
    """The notable entities extracted from one session."""

    session_id: str
    entities: list[Entity]


# ---------------------------------------------------------------------------
# Audio synthesis (TTS)
# ---------------------------------------------------------------------------


class SynthesizeRequest(BaseModel):
    """Ask for a spoken clip. ``script_text`` is the exact narration to voice."""

    kind: ArtifactKind
    script_text: str
    voice: NarratorVoice


class AudioArtifact(BaseModel):
    """A synthesized clip, returned as bytes (base64 WAV). The worker has no disk, so the agent
    writes these bytes onto the user's machine (e.g. into the static site) via a callback tool
    rather than the worker saving a file. Clips are short (recaps/intros), so base64 is fine."""

    artifact_id: str
    kind: ArtifactKind
    voice: str
    script_text: str
    audio_base64: str
    duration_s: float


# ---------------------------------------------------------------------------
# Notification (pluggable channel; see notifier.py)
# ---------------------------------------------------------------------------


class NotifyRequest(BaseModel):
    title: str
    message: str


class NotificationResult(BaseModel):
    delivered: bool
    channel: str
    title: str
    message: str


# ---------------------------------------------------------------------------
# SessionScribe subagent boundary (map-reduce: one child per session)
# ---------------------------------------------------------------------------
# These are the typed inputs/outputs of the ChroniclerScribe CHILD agent's accepts handlers.
# subagent_toolset turns each handler into a parent tool with these exact models, so the digest
# a child returns is validated back into SessionDigest on the parent side — typed across the
# subagent boundary.


class ScribeTask(BaseModel):
    """Tell a SessionScribe which session to process (transcribe + summarize + extract).

    ``file_ref`` is the Gemini upload the CONDUCTOR obtained for this session's recording (via the
    ``upload_recording`` callback on its own machine-facing bridge) and threads down to the child —
    the scribe is a child workflow the bridge can't reach, so the parent supplies the audio handle."""

    session_id: str
    file_ref: GeminiFileRef


class SessionDigest(BaseModel):
    """A SessionScribe's full analysis of one session — the unit the parent reduces over."""

    session_id: str
    transcript: TranscriptMeta
    summary: SessionSummary
    entities: CampaignEntities


class ScribeQuestion(BaseModel):
    """Ask a SessionScribe a question answered from its session's transcript."""

    session_id: str
    question: str


class ScribeAnswer(BaseModel):
    """A SessionScribe's answer to a question about its session, grounded in the transcript."""

    session_id: str
    question: str
    answer: str
