"""Local-filesystem callback tools that give the Chronicler access to the DM's OWN machine.

The Chronicler agent is a durable Temporal workflow that may run anywhere — picture a cloud
worker — and has **no filesystem of its own**. These six tools are ``@agent.callback_tool_defn``
tools: each has NO worker-side body. When the model (inside Code Mode) calls one, the tool call
**pauses inside the workflow** and publishes a ``callback_requested`` event; the thin local bridge
running on the DM's laptop (``local_bridge.py``) executes the operation against a local directory
and posts the result back via the ``provide_callback_result`` update, and the agent resumes. The
agent never touches a filesystem — it just calls these like any other host function.

This is how a *deployed* Chronicler gets *local context*: the DM keeps campaign notes, character
sheets, and house rules on their own disk, and the agent reads them here; it can also WRITE a
static campaign website back to that same disk. Which side effects run on the laptop is decided
entirely by which tools the bridge implements.

NB: no ``from __future__ import annotations`` here. The parameter + return annotations are read
directly to build the model-facing tool schemas (via Code Mode's stub generation) and the
callback output-type validator, so they must be concrete types, not stringized. Bodies must be a
lone ``...`` — the harness enforces this at import time.
"""

from temporal_agent_harness.harness import agent

from .chronicler_models import (
    GeminiFileRef,
    IngestResult,
    SessionList,
    SessionRef,
    Transcript,
)


@agent.callback_tool_defn()
async def ls(path: str) -> list[str]:
    """List the entries directly under a directory on the DM's local machine, one per entry. Pass
    "." for the root of the local campaign directory. Sub-directory entries end with a trailing
    "/". Use this to see what already exists in a folder before deciding where new content (a
    campaign page, a recap, an asset) should go."""
    ...


@agent.callback_tool_defn()
async def tree(path: str) -> str:
    """Return an indented tree of the local subtree rooted at `path` (pass "." for the whole local
    campaign directory), so you can understand how the DM's files and any site you've built are
    currently organized before adding, editing, or moving anything. Directories are shown with a
    trailing "/"."""
    ...


@agent.callback_tool_defn()
async def read_file(path: str) -> str:
    """Read a UTF-8 text file from the DM's local machine and return its full contents. Use it to
    pull in local campaign context — notes, character sheets, house rules — or to read a page of a
    site you are building before revising it. Always read a file before editing it so you can
    revise the existing text instead of clobbering it. `path` is relative to the local campaign
    root, e.g. "notes/party.md" or "site/index.html"."""
    ...


@agent.callback_tool_defn()
async def write_file(path: str, content: str) -> str:
    """Create a new text file, or OVERWRITE an existing one, on the DM's local machine with
    `content` (UTF-8), creating parent directories as needed. This replaces the whole file — to
    add to an existing file, read_file it first and write back the full, revised contents. Use it
    to build a static campaign website (HTML/CSS/Markdown) or to save recaps and summaries to the
    DM's disk. Returns a short confirmation. `path` is relative to the local campaign root, e.g.
    "site/sessions/session-01.html". Text only — binary assets are handled separately."""
    ...


@agent.callback_tool_defn()
async def delete_file(path: str) -> str:
    """Delete a file from the DM's local machine. Returns a short confirmation. Use sparingly —
    only when a file is truly obsolete or the DM asks for it. `path` is relative to the local
    campaign root."""
    ...


@agent.callback_tool_defn()
async def grep(pattern: str) -> list[str]:
    """Search every text file under the local campaign directory for a Python regular expression,
    returning matching lines as "path:lineno: line". Use it to find where a topic already lives on
    the DM's disk before creating a new file for it (so related notes and pages stay together)."""
    ...


# The generic filesystem toolset, in a stable order.
LOCAL_FS_TOOLS = [ls, tree, read_file, write_file, delete_file, grep]


# ---------------------------------------------------------------------------
# Session + audio callbacks — the stateless worker keeps NO recordings, registry, transcripts, or
# artifacts. These all live on the DM's machine; the bridge owns the registry, uploads recordings
# to the Gemini Files API for the worker to transcribe from, and persists transcripts + audio.
# ---------------------------------------------------------------------------


@agent.callback_tool_defn()
async def write_binary_file(path: str, content_base64: str) -> str:
    """Write raw bytes (base64-encoded) to a file on the DM's machine, creating parent
    directories as needed. Use this to drop synthesized audio (from `synthesize_audio`, whose
    `audio_base64` you pass here) into the static site, e.g. "site/audio/recap.wav". For text
    files use `write_file`. Returns a short confirmation. `path` is relative to the local root."""
    ...


@agent.callback_tool_defn()
async def list_sessions(campaign_id: str | None = None) -> SessionList:
    """List recorded sessions on the DM's machine in play order. Pass a `campaign_id` to filter to
    one campaign, or `null` to list ALL sessions across every campaign — each SessionRef includes
    its `campaign_id`, so passing null is how you discover what campaigns exist (don't guess a
    name). `transcribed` tells you which already have a transcript saved. Also reports
    `unregistered_files`: recordings on disk not registered yet — offer to ingest them."""
    ...


@agent.callback_tool_defn()
async def ingest_sessions(campaign_id: str) -> IngestResult:
    """Scan the DM's recordings for audio not yet in the registry, register any new ones under the
    given campaign (auto-assigning id, play-order number, and a title from the filename), and
    return what was added plus the campaign's full session list. Call this to pick up recordings
    the DM just added — no manual registry editing needed."""
    ...


@agent.callback_tool_defn()
async def save_recording(campaign_id: str, title: str, audio_base64: str) -> SessionRef:
    """Save a recording (base64 WAV bytes) onto the DM's machine and register it under `campaign_id`
    with `title`, auto-assigning a session id and play-order number. Use this to persist a sample
    from `generate_sample_audio` (pass its `audio_base64` and `title`). Returns the new session."""
    ...


@agent.callback_tool_defn()
async def upload_recording(session_id: str) -> GeminiFileRef:
    """Upload a registered session's local recording to the Gemini Files API (done on the DM's
    machine, so the worker never handles the audio) and return a reference to transcribe from.
    Call this to get the `file_ref` for `transcribe_recording`. Waits until the upload is ready."""
    ...


@agent.callback_tool_defn()
async def save_transcript(transcript: Transcript) -> str:
    """Persist a full transcript on the DM's machine (the durable source of truth), keyed by its
    `session_id`. Call this right after `transcribe_recording` so the transcript survives even
    though the worker keeps nothing. Returns a short confirmation."""
    ...


@agent.callback_tool_defn()
async def read_transcript(session_id: str) -> Transcript:
    """Read a previously saved transcript back from the DM's machine. Use this when you need to
    summarize/extract/answer for a session the current worker didn't just transcribe — pass the
    returned `full_text` to those tools as `transcript_text`."""
    ...


# Session + audio callbacks, in a stable order.
LOCAL_SESSION_TOOLS = [
    write_binary_file,
    list_sessions,
    ingest_sessions,
    save_recording,
    upload_recording,
    save_transcript,
    read_transcript,
]

# Everything the local bridge fulfills — generic filesystem + session/audio. Handed to Code Mode
# as host functions and used by the bridge to build its name -> implementation dispatch.
LOCAL_TOOLS = [*LOCAL_FS_TOOLS, *LOCAL_SESSION_TOOLS]
