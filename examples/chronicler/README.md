# Chronicler audio workflow

Chronicler is a review-first example for turning a D&D transcript into a spoken WAV recap. The
browser owns source discovery and local writes; the worker is stateless and runs only the
conversational parent, the model-free audio child, and Gemini TTS synthesis.

The safety boundary is deliberate:

- Select a persisted transcript or enter a topic for a clearly marked synthetic transcript.
- Review the complete source, exact narration, fixed `Charon` voice, and destination paths.
- Reprepare changes before approval; a change produces a new review identity.
- Approve generation explicitly. The `generate_audio` operation always requires approval and is
  not exposed to the chat model.
- The audio child performs create-only browser writes beneath the connected folder, handles late
  collisions with a destination-only revision, and recovers only the same approved package.
- Playback begins only after receipt, binding, size, SHA-256, and WAV-header verification.

## Run locally

Requirements: `uv`, `pnpm`, the Temporal CLI, and a Chromium-based browser with File System
Access, IndexedDB, and Web Locks.

```bash
cd examples/chronicler
just setup
# Add GEMINI_API_KEY to .env.local
just dev
```

Open <http://localhost:8000>, select **Chronicler**, choose **Connect folder**, and select
`examples/chronicler/local` (created by `just setup`) or another campaign folder. The connected
tab must remain active while it fulfills local operations. A historical replay is read-only.

Connection settings come from `TEMPORAL_CONFIG_FILE` and `TEMPORAL_PROFILE`. By default the
example uses the repository's `temporal.local.toml`. Run individual services with `just temporal`,
`just session-manager`, `just server`, and `just worker`.

## Browser bridge and local-operation safety

Chromium users connect a directory through **Connect folder**. The browser owns source discovery
and local writes. It persists the selected `FileSystemDirectoryHandle` and unsubmitted operation
outcomes in IndexedDB; Web Locks elect one active tab as the operation leader, so only that tab
executes local operations. The active leader must remain open while the example fulfills them.

Each selected directory gets an immutable local binding ID. The UI will not switch folders while
an operation is pending, running, or in the durable outbox. Bridge and root IDs route work only;
they are not credentials. A production deployment needs authenticated pairing and server-side
authorization, such as a server-issued bridge/workflow subscription or pairing registry.

An `idempotency_key` represents one semantic side effect across runs that reuse a workflow ID.
The browser outbox partitions it by workflow ID, route root, immutable folder binding, and key,
and rejects a reused key whose arguments or output schema differ. The current transport has no
separate run ID; if it later does, execution identity belongs in that partition.

Playback is enabled only after the local receipt, folder binding, byte size, SHA-256, and WAV
header all verify. The audio child performs create-only writes under the connected folder; late
collisions receive a destination-only revision, and recovery can resume only the approved package.

## Example UI commands

The example UI owns its build, test, check, and development commands while sharing dependencies
installed by `just app-install`:

```bash
cd examples/chronicler
pnpm --dir ui run build
pnpm --dir ui run test
pnpm --dir ui run check
pnpm --dir ui run dev
```

## Create a recap

1. In **Create spoken recap**, select an existing transcript discovered in the connected folder,
   or enter a topic to draft a synthetic transcript.
2. Review the exact source, narration, `Charon` voice, and deterministic destinations. A WAV is
   written beneath `audio/`; synthetic sources also receive a sibling Markdown transcript.
3. Use **Reprepare review** for changes. This invalidates the previous review approval.
4. Choose **Approve and generate**, then approve the non-rememberable `generate_audio` operation.
5. Follow the child through generating, saving, and completion. If a create-only collision occurs,
   approve new destinations without changing the transcript or narration.
6. Recover failed or interrupted work only when offered; cancellation is terminal. Use
   **Verify and retry playback** when local verification needs to run again.

Gemini TTS defaults to `gemini-2.5-flash-preview-tts`; override it with
`CHRONICLER_TTS_MODEL`. Source text and audio use the configured large-payload offload driver.

## Audio-only module boundary

| File | Role |
|---|---|
| `app.py`, `agents.toml` | Serve the Chronicler-specific UI bundle and audio API with one Chronicler agent entry. |
| `session_manager_worker.py` | Host the packaged session manager. |
| `conversational_workflow.py` | Tool-free chat plus prepare, reprepare, approve/start, and recover authority. |
| `audio_models.py` | Immutable review, approval, destination, receipt, cancellation, and recovery contracts. |
| `audio_tool.py` | Non-rememberable approval boundary and fixed-ID child launcher. |
| `audio_workflow.py` | Model-free synthesis/write/collision/cancellation/recovery orchestration. |
| `audio_activities.py` | Standalone exact-script Gemini TTS and WAV validation. |
| `worker.py` | Register exactly the parent, audio child, synthesis activity, Gemini plugin, and payload codec. |
| `wait_for_temporal.py` | Prevent local process startup races. |

Runtime data in `local/`, `sessions/`, and `.env.local` is intentionally untracked and is never
removed by source cleanup.
