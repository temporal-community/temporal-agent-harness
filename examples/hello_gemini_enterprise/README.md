# Hello Gemini Enterprise

A hello-world Gemini agent — one tool, plain-text chat — that runs **unchanged** against either
Gemini backend:

- the **consumer Gemini Developer API** (`generativelanguage.googleapis.com`, API key), or
- Google's **Gemini Enterprise Agent Platform** (GEAP — the 2026 rebrand of Vertex AI;
  `aiplatform.googleapis.com`, Application Default Credentials).

It exists to answer one question cheaply: *what does it actually take to move a harness Gemini
agent onto GEAP?* It is the smallest thing that still exercises the real surface — the **Interactions
API** tool-calling loop, the same one Monty, the wiki keeper, and the internal QA prototype use.

## The verdict (measured 2026-08-10)

**GEAP hosts the Interactions API, but not in a form an agent like this can use.** Model
interactions are refused outright, and agent interactions can't be built over an ordinary Gemini
model. So an Interactions-API agent **cannot** be moved to GEAP by configuration alone today.

All measurements: project `gemini-ai-dev-464305`, location `global`, model `gemini-3.5-flash`,
`google-genai` 2.8.0, Agent Platform API enabled.

| Call | Result |
|---|---|
| `models.generate_content` | ✅ **works** — real multimodal answer (Google's own GEAP sample, verbatim) |
| `agents.list()` | ✅ **works** — returns an empty list, so the Agents surface is live |
| `interactions.create(model=…)` | ❌ `400 'Unsupported model interaction: gemini-3.5-flash'` |
| `interactions.create(agent=<a model id>)` | ❌ `400 "'gemini-3.5-flash' refers to a model, but was provided in the 'agent' field. Please use the 'model' field instead."` |
| `agents.create(base_agent=<a model id>)` | ❌ `400 "Only 'antigravity-preview-05-2026' is allowed as a base_agent value."` |

Read those last three together — they're what makes this conclusive rather than suggestive:

1. Sent as `agent=`, the service **recognizes the string as a model** and redirects you to the
   `model` field.
2. Sent as `model=`, the very same string is **declined as an unsupported capability**.
3. And you cannot escape via a custom Agent, because `base_agent` accepts exactly one value —
   `antigravity-preview-05-2026` — not arbitrary Gemini models.

The service knows the model, routes it, names the right field, and then refuses the operation.
That is a deliberate capability gap, not a lookup failure.

**Ruled out**, each by direct measurement:

- **Naming.** Eight forms, all identical failures: bare, `models/…`, `google/…`,
  `publishers/google/models/…`, `projects/<p>/locations/<l>/publishers/google/models/…`,
  `projects/<p>/locations/<l>/models/…`, the full `//aiplatform.googleapis.com/…` resource name,
  `vertex_ai/…`. The error echoes whatever string you send.
- **Model choice.** `gemini-3.5-flash`, `gemini-3-flash`, `gemini-3-pro`, `gemini-3.5-pro`,
  `gemini-2.5-flash`, `gemini-2.5-pro`, `gemini-flash-latest` — all identical.
- **The `enterprise` vs `vertexai` flag.** They are aliases, not modes:
  `resolved_vertexai = enterprise if enterprise is not None else vertexai` in
  `google/genai/client.py`, whose own docstring calls `vertexai` the legacy flag. Both resolve to
  `base_url=https://aiplatform.googleapis.com/`. (Same story for the `GOOGLE_GENAI_USE_ENTERPRISE`
  / `GOOGLE_GENAI_USE_VERTEXAI` env vars.)
- **Provisioning.** Worth knowing if you re-run this: for a few minutes after the API is first
  enabled, `agent=` returns `'Resource setup has just started. Please try again shortly.'` That
  message is transient and **not** the same finding — wait for it to clear before concluding
  anything. All results above are post-provisioning.
- **Credentials / project / API enablement / model access.** `models.generate_content` succeeding
  on the identical client covers all four.

Consistent with [Google's forum thread](https://discuss.ai.google.dev/t/is-interactions-api-available-in-vertexai/114356),
where a Google rep said in Jan 2026 that Interactions-on-Vertex was a couple of months out and users
were still waiting as of 2026-07-19.

**Still untested:** an actual interaction against `antigravity-preview-05-2026` (create an Agent on
that base, then `interactions.create(agent=…)`). Skipped on cost grounds and because a coding-agent
base isn't the shape a harness agent needs — it would confirm the agent-interaction path is
functional, but wouldn't change the conclusion for a Gemini-model agent.

### What this means for a real migration

The client swap below is correct and remains the whole *configuration* story — but for an
Interactions-API agent it is necessary and **not sufficient**. Porting
`internal-ai-prototypes/agent` to GEAP today means:

1. **Moving its inner loop from `interactions.create` to `models.generate_content`.** The harness
   already supports this via `google_genai_client(vertexai=True, project=…, location=…)`, and it
   demonstrably works on GEAP. This is the real work: rewriting the SSE step/delta reduction as a
   `generate_content` tool-calling loop.
2. **Replacing the built-in `file_search` tool**, which has no drop-in GEAP equivalent (that's
   Vertex RAG Engine / Vertex AI Search) — a separate investigation regardless of surface.
3. **Accepting an observability regression, or fixing it.** The harness's `generate_content` path
   currently publishes only `reply_delta` — no `model_interaction_*` bracket, no `tool_requested`,
   unlike the Interactions path. Porting without addressing that loses the model spans and token
   accounting.

The cheaper alternative is to wait: nothing about the harness or the prototype needs to change if
Google ships model interactions on GEAP, since the client swap already works.

## The configuration story (verified, and still the whole of it)

**The backend choice is worker-side. There is no workflow change.**

`workflow.py` never names a backend. It calls `gemini.interactions.create(...)` on the harness's
Temporal-aware shim, which forwards the kwargs into the plugin's activity; the real `genai.Client`
constructed in `worker.py` is what resolves the endpoint and the credentials. So the switch is:

```python
# consumer Gemini Developer API
GeminiClient(api_key=os.environ["GEMINI_API_KEY"])

# GEAP / Agent Platform  (`enterprise=` is the current alias for the legacy `vertexai=`)
GeminiClient(enterprise=True, project=project, location="global")
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
