# Hello OpenAI file_search

A doc-QA agent on OpenAI's **hosted `file_search`**, over a small vector store the worker ingests at
startup. The counterpart to [`examples/hello_gemini_enterprise`](../hello_gemini_enterprise): that
one asks whether a harness Gemini agent can move to GEAP; this one answers the question that
actually decides the migration —

> The Gemini Interactions API's built-in `file_search` is what the main doc-QA agent is built on, and
> GEAP **refuses to serve it**. Is OpenAI's hosted `file_search` a drop-in for it?

## The finding: yes, and it costs nothing harness-side

**Proven end to end.** The corpus states a figure no model could know or guess (`corpus.py`), and the
agent returns it:

```
[file_search] reply: 'The Zarnak coefficient for the 2026-Q3 reference build is **47.3 milliseconds**.'
```

That *is* the proof — a plausible-sounding guess cannot pass, so no instrumentation of OpenAI's
server-side tool span is needed to be sure retrieval ran.

**Nothing in the harness had to change.** The vendored OpenAI plugin already forwards `FileSearchTool`
through to the model activity (it's in the handled tool union in both `_temporal_model_stub.py` and
`_invoke_model_activity.py`), so using hosted retrieval is a one-line addition to the agent's `tools`
list:

```python
tools=[
    FileSearchTool(vector_store_ids=[VECTOR_STORE_ID]),   # hosted, server-side
    as_openai_agent_tool(self._runner, get_weather),      # harness-owned, via run_tool
]
```

Ingestion is ordinary OpenAI API work done once at worker startup (`corpus.ensure_vector_store`) —
create a vector store, upload the file, `upload_and_poll` until it's indexed. No new library, no
corpus/datastore plumbing.

### The full event vocabulary survives

One turn on this path publishes everything:

```
turn_started, model_interaction_started, tool_requested, model_interaction_ended,
tool_start, tool_end, model_interaction_started, reply_delta ×10,
model_interaction_ended, reply, turn_end
```

Both model calls bracketed, token usage on the `ended` events, `tool_requested`, the tool lifecycle,
and streamed deltas. Compare the same agent on GEAP's `generate_content`, which manages only
`turn_started, tool_start, tool_end, reply, turn_end` — no model spans, no token accounting, no
`tool_requested`. Asserted in `tests/examples/hello_openai_file_search/test_file_search.py`, not
assumed.

### The one real constraint: hosted ≠ harness-owned

`file_search` runs inside OpenAI's backend, so it does **not** pass through `run_tool`. Consequences,
both asserted by the test rather than hoped for:

- **No approval gate.** You cannot require human sign-off on a hosted retrieval call.
- **No `tool_start` / `tool_end` for it.** Hosted tool spans are deferred (harness spec §11), so the
  retrieval step is invisible in the tool events — only `get_weather`, the harness-owned tool, shows
  up there.

That's why `get_weather` is in this example at all: it's the control. One turn, two kinds of tool,
so the difference in what the harness sees is visible side by side.

If retrieval must be gateable or fully observable, it has to be a **harness tool** instead — an
`@agent.activity_tool_defn async def search_docs(query)` over whatever index you like. More code, but
it gets the approval gate and full lifecycle, and it's provider-agnostic (see the GEAP example's
README).

## Configure

Set in the repo-root `.env.local`:

| Variable | Meaning |
|---|---|
| `OPENAI_API_KEY` | Required — used for both model calls and vector-store setup. |
| `OPENAI_VECTOR_STORE_ID` | Optional. Reuse an existing store instead of creating one. |

The worker resolves the store (idempotent by name, `harness-hello-file-search`), then **exports**
`OPENAI_VECTOR_STORE_ID` before it starts polling.

> **Why the environment and not a module assignment?** The Temporal workflow sandbox re-imports the
> workflow module into its own namespace, so setting `workflow.VECTOR_STORE_ID` from the worker is
> invisible to the running workflow — an earlier draft did that and every turn failed with
> `MissingVectorStore`. The environment is process-global, so the sandbox's re-import does see it.
> Same trade as `react_agent`'s streaming toggle: fixed per worker process, and not recorded in
> workflow history, so keep it stable for a session's lifetime.

## Run

Headless proof, no UI:

```bash
just prove
```

Or the full stack, four terminals from this directory:

```bash
just temporal          # 1. local Temporal dev server (or bring your own)
just session-manager   # 2. packaged session-manager worker
just server            # 3. FastAPI API + UI on http://localhost:8000
just worker            # 4. this example's agent worker (ingests on first run)
```

Then open <http://localhost:8000>, pick **Hello OpenAI file_search**, and ask *"What is the Zarnak
coefficient for the 2026-Q3 reference build?"*. Ask about the weather instead to watch a
harness-owned tool's lifecycle events.

`just stores` lists the account's vector stores if you want to clean up; `just config` shows what the
worker will use.
