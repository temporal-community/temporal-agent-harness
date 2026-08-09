# Hello Gemini Enterprise

A hello-world Gemini agent — one tool, plain-text chat — that runs **unchanged** against either
Gemini backend:

- the **consumer Gemini Developer API** (`generativelanguage.googleapis.com`, API key), or
- Google's **Gemini Enterprise Agent Platform** (GEAP — the 2026 rebrand of Vertex AI;
  `aiplatform.googleapis.com`, Application Default Credentials).

It exists to answer one question cheaply: *what does it actually take to move a harness Gemini
agent onto GEAP?* It is the smallest thing that still exercises the real surface — the **Interactions
API** tool-calling loop, the same one Monty, the wiki keeper, and the internal QA prototype use.

## The finding

**The migration is worker-side configuration. There is no workflow change.**

`workflow.py` never names a backend. It calls `gemini.interactions.create(...)` on the harness's
Temporal-aware shim, which forwards the kwargs into the plugin's activity; the real `genai.Client`
constructed in `worker.py` is what resolves the endpoint and the credentials. So the switch is:

```python
# consumer Gemini Developer API
GeminiClient(api_key=os.environ["GEMINI_API_KEY"])

# GEAP / Agent Platform
GeminiClient(vertexai=True, project=project, location="global")
```

That's it — same plugin, same activities, same workflow, same event history. A running session
cannot tell which one it is on. Google rebranded Vertex AI to the Gemini Enterprise Agent Platform
at Cloud Next 2026 and migrated SDKs, APIs and billing **with no breaking changes**, which is why
`vertexai=True` is still the switch even though the product name changed.

Note that the workflow does *not* pass `vertexai=`/`project=` to `google_genai_client(...)`. Those
arguments only affect the SDK's URL formatting for the `models.*` path, which an Interactions-API
agent never touches. If you write a `models.generate_content` agent instead, pass them there too so
the workflow-side formatting matches the worker's client.

### Two things that are *not* free

1. **The Agent Platform API must be enabled on the project.** Otherwise the model activity fails
   with `403 SERVICE_DISABLED`, naming the project and an activation URL:
   ```
   just enable-geap my-project      # gcloud services enable aiplatform.googleapis.com
   ```
2. **Model availability differs between the two backends.** A model id served on the consumer API
   is not automatically served on GEAP. `DEFAULT_MODEL` in `workflow.py` is the one thing besides
   the client you may have to re-check. (It's a module constant, not an env var, on purpose:
   reading the environment from workflow code would make the model a non-deterministic input that
   isn't recorded in history.)

### Credentials never reach the workflow

Worth saying explicitly, because it's what makes the swap safe: in **both** modes the worker is the
only process that authenticates. The consumer API's key and GEAP's ADC bearer token are used inside
the activity; neither enters workflow code or Temporal's event history. Switching backends does not
change that story, it just changes which credential the activity uses.

## Configure

Set these in the repo-root `.env.local` (see `.env.example`):

| Variable | Mode | Meaning |
|---|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | both | `true` → GEAP; unset/false → consumer API. (The google-genai SDK reads this name itself, so it's the SDK's idiom.) |
| `GEMINI_API_KEY` | consumer | Required for the consumer Gemini Developer API. |
| `GOOGLE_CLOUD_PROJECT` | GEAP | Required — the project GEAP authorizes and bills against. |
| `GOOGLE_CLOUD_LOCATION` | GEAP | Region, or `global` (default). `global` → `aiplatform.googleapis.com`; a region → `<region>-aiplatform.googleapis.com`. |

GEAP also needs Application Default Credentials on the **worker host**:

```bash
gcloud auth application-default login
```

`just backend` prints which backend the worker will use and whether its credentials are present.
The worker also prints the resolved backend in its startup line, so a run is never ambiguous.

## Run

Four terminals, from this directory:

```bash
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. FastAPI API + UI on http://localhost:8000
just worker            # 4. this example's agent worker
```

Open http://localhost:8000, pick **Hello Gemini Enterprise**, and ask *"What's the weather in
Paris?"*. The reply streams token by token and the `get_weather` tool call appears live on the turn
stream.

### A/B the two backends

`just worker-geap <project>` runs the worker against GEAP without editing `.env.local`. Stop it,
run plain `just worker` (consumer API), and compare — same workflow code, same UI, same event
vocabulary. That side-by-side *is* the proof.

## What this agent is (and isn't)

One inline harness tool (`get_weather`, a canned lookup), a system prompt, and the Interactions
tool-calling loop the workflow drives itself — the Interactions API has no automatic function
calling, so the loop is the agent. Tool calls still go through `runner.run_tool`, so the harness
keeps approval-policy evaluation and the `tool_start` / `tool_end` / `tool_error` turn events.

Deliberately **not** covered here, because they are separate migration questions:

- **`file_search` and file-search stores.** The internal QA prototype leans on the Interactions
  API's built-in `file_search` tool. That is not the same feature as GEAP's retrieval offerings, so
  moving it is its own investigation — not something `vertexai=True` solves.
- **Durable subagents, Code Mode, callback tools.** See `examples/monty` and
  `examples/callback_tools`; they are orthogonal to the backend question.
