# QaAgent example

Runs the shared harness UI/API against a `QaAgent` worker, wherever that worker happens to be
running (this repo doesn't host it - see `agents.toml` for the expected `workflow_type` /
`task_queue`).

## Setup

1. **`temporal.toml`** - at the repo root, create a private, gitignored `temporal.toml` with a
   profile pointing at the same namespace the `QaAgent` worker connects to (Temporal Cloud or your
   own server):

   ```toml
   [profile.cloud]
     address = ""     # e.g. "my-namespace.a1b2c.tmprl.cloud:7233"
     namespace = ""   # e.g. "my-namespace.a1b2c"
     api_key = ""
   ```

2. **`.env.local`** - at the repo root, copy `.env.example` to `.env.local` and set:

   ```
   TEMPORAL_CONFIG_FILE=temporal.toml
   TEMPORAL_PROFILE=cloud
   ```

   Run `just config` from this directory anytime to confirm which file/profile is active.

## Run

Each command in its own terminal, from this directory (`examples/qa_agent`):

```bash
just session-manager   # session-manager worker
just server             # FastAPI API + built Svelte UI -> http://localhost:8000
```

Skip `just temporal` - that starts a *local* dev server, not needed when pointing at an existing
namespace.

## Open the UI

<http://localhost:8000> - the `QaAgent` sessions started/discovered there will show up once the
worker (wherever it runs) is polling the `qa-agent` task queue on that same namespace.
