# Chronicler — a D&D campaign archivist example

A Temporal-native agent that remembers your campaign. You chat in plain text; a **model in the
loop** converses, then **writes a Python script and runs it** (via the harness's Code Mode) to
transcribe session recordings, summarize them, track NPCs/locations/quests, and synthesize spoken
"previously on…" recaps — replying in prose. Every step is a **durable, typed, approval-gated
Gemini-backed activity**.

## Why this makes a good harness example

- **Real long-running work.** Transcribing a multi-hour session is a genuinely long, retryable,
  heartbeating activity — crash the worker mid-transcribe and it resumes. (Not simulated latency.)
- **Typed inputs and outputs, enforced.** Every tool's arguments and return are pydantic models
  (`chronicler_models.py`). Code Mode reflects over them to generate the sandbox's host-function
  stubs, and the model-authored script is **statically type-checked against them before it runs** —
  a wrong field name is an error to fix, not a bad result. The *same* models are used as the
  Gemini structured-output schema and the tool return type.
- **Code does the heavy lifting, not the LLM.** The model orchestrates; the durable tools do the
  transcription/summarization/TTS. Heavy data (full transcripts) stays worker-side, addressed by
  `session_id`, so it never bloats the model's context.
- **Large-payload offload** used for its real purpose (audio + transcripts > Temporal's ~2 MB).
- **Concurrency over real work:** the script can `asyncio.gather` transcriptions/summaries of
  several sessions at once.
- **"Long job done → ping me."** A long transcription completes → the agent calls a pluggable
  `notify(...)` tool (see `notifier.py`): in-app by default, or a real webhook out-of-band.

## Run — zero to running

From this directory:

```bash
just setup     # creates .env.local from the template (then set GEMINI_API_KEY in it)
just dev       # builds the UI and runs the WHOLE stack in one terminal
```

`just dev` starts temporal + session-manager + server + worker together via [honcho](https://github.com/nickstenning/honcho), interleaving their logs; one Ctrl-C stops everything. The two workers auto-restart on code changes (`watchfiles`) and the server hot-reloads. It self-heals the env: if `.env.local` is missing it creates it, and it fails fast with a clear message if `GEMINI_API_KEY` isn't set. Then open <http://localhost:8000>.

- `GEMINI_API_KEY` — required. Get one at <https://aistudio.google.com/apikey>. The free tier has low daily quotas; enable billing for sustained use.
- `TEMPORAL_CONFIG_FILE` defaults to the repo's committed `temporal.local.toml` (a local dev server). For your own server / Temporal Cloud, create a private `temporal.toml` and point `TEMPORAL_CONFIG_FILE` at it (see `.env.example`).
- Bring your own Temporal? Run a subset: `honcho start session-manager server worker`.
- Model names are overridable (`CHRONICLER_TRANSCRIBE_MODEL`, `CHRONICLER_SUMMARY_MODEL`, `CHRONICLER_TTS_MODEL`) — availability varies by API access.

### Run processes individually (alternative)

Each in its own terminal, all from this directory: `just temporal`, `just session-manager`, `just server`, `just worker`. `just seed` generates a sample recording from the CLI.

You don't even need `just seed` — with the stack running, just **ask the agent** *"generate a
sample session"* and it creates a short synthetic recording for you (the `generate_sample_session`
tool); `just seed` is the CLI equivalent. To use real recordings, drop audio files in `sessions/`
and ask it to *"register new recordings"* (the `ingest_sessions` tool), or add entries to
`sessions/sessions.json` by hand.

Then open <http://localhost:8000>, pick **Chronicler**, and try:

> *"Transcribe session 1, then give me a spoken 'previously on' recap."*

You'll watch it write a script, run `transcribe_session` (the long, heartbeating step), `notify`
you it's ready, `summarize_transcript`, and `synthesize_audio` — each a durable, approval-gated
tool call visible in the UI.

## Notifications

Set `CHRONICLER_NOTIFIER=webhook` + `CHRONICLER_WEBHOOK_URL=<slack/discord/...>` in `.env.local`
to get a real out-of-band ping when a transcription finishes; leave it `inapp` (default) for the
in-UI notification. Adding a channel is a new `Notifier` in `notifier.py` and nothing else.

## Files

| File | Role |
|------|------|
| `chronicler_models.py` | Typed pydantic boundaries — the whole contract |
| `chronicler_activities.py` | Durable Gemini audio tools (transcribe/summarize/extract/synthesize/notify) + worker-side transcript cache |
| `notifier.py` | Pluggable notification channel (in-app default, webhook opt-in) |
| `conversational_workflow.py` | `ChroniclerAgent` — Gemini tool-calling loop with one Code Mode tool |
| `worker.py` | Hosts the agent + activities + Code Mode + Gemini plugin + offload codec |
| `session_manager_worker.py` | Packaged session-manager (generic) |
| `app.py` / `agents.toml` | FastAPI app + agent registry the UI reads |
| `seed_session.py` | Generates a sample session recording via TTS |
