# Migrating off the Gemini Interactions API: GEAP vs OpenAI

**Question.** `internal-ai-prototypes/agent` is a doc-QA agent built on the **Gemini Interactions
API** with its **built-in `file_search`**. That combination has to move, soon, for legal reasons. The
two candidate destinations are Google's **Gemini Enterprise Agent Platform** (GEAP, the 2026 Vertex
AI rebrand) and **OpenAI**. Which is less work?

**Answer, measured: OpenAI, by a wide margin — and the gap is almost entirely retrieval, not the
agent loop.** Both destinations require rewriting the inner loop, and that rewrite is cheap and
proven either way. What separates them is that OpenAI's hosted `file_search` is a genuine drop-in
for what the agent uses today, while GEAP's retrieval is a different product with a new library, a
new failure mode, and regional constraints that reach back and constrain the model choice.

Everything below was measured on 2026-08-10 against project `gemini-ai-dev-464305` with
`google-genai` 2.8.0, `openai` 2.45.0, `openai-agents` 0.18.1. Reproduce with:

```bash
# GEAP: agent loop
GOOGLE_GENAI_USE_ENTERPRISE=true GOOGLE_CLOUD_PROJECT=<project> \
  uv run pytest tests/examples/hello_gemini_enterprise -v -s
# GEAP: retrieval
uv run --group examples python -m examples.hello_gemini_enterprise.rag_engine_probe \
  --project <project> --demo-silent-failure
# OpenAI: agent loop + retrieval
uv run pytest tests/examples/hello_openai_file_search -v -s
```

## The blocker that starts all of this

GEAP **does not serve the Interactions API's model-interaction kind**, and it **hard-blocks
`file_search` by mode**:

| Call on GEAP | Result |
|---|---|
| `interactions.create(model=…)` | ❌ `400 'Unsupported model interaction: <model>'` — every model id, every name form |
| `interactions.create(agent=<model id>)` | ❌ *"refers to a model, but was provided in the 'agent' field. Please use the 'model' field instead."* |
| `agents.create(base_agent=<model id>)` | ❌ *"Only 'antigravity-preview-05-2026' is allowed as a base_agent value."* |
| `file_search_stores.list()` | ❌ *"only supported in Gemini Developer API mode, not in Gemini Enterprise Agent Platform mode"* |
| `Tool(file_search=…)` | ❌ same |
| `models.generate_content` | ✅ works |
| `Tool(retrieval=vertex_rag_store / vertex_ai_search)` | ✅ works |

So "keep the code, add `vertexai=True`" is not available. Both options are ports.

## Side by side

| | **CURRENT → OpenAI** | **CURRENT → GEAP** |
|---|---|---|
| **Inner loop** | Port to the Agents SDK. `Runner.run_streamed` drives the loop; harness tools attach with `as_openai_agent_tool`. | Port to `models.generate_content` with a hand-driven loop. ✅ **proven** (`workflow_generate_content.py`) |
| **Loop difficulty** | Low. Three existing examples do exactly this. | Low. The reduction gets *simpler* than Interactions: finished `Part`s instead of an SSE step/delta fold with argument-fragment reassembly (~70 lines removed). |
| **Conversation state** | `result.to_input_list()` — the SDK's own pattern. | You own a `list[Content]`, appending model turns and function responses. Transcript moves into workflow state and history; context-window management becomes yours. |
| **Retrieval** | ✅ **proven.** Hosted `file_search` + vector stores — a true analogue of Interactions' `file_search`. | ✅ **proven**, but as a different product: RAG Engine via `Tool(retrieval=vertex_rag_store=…)`. |
| **Retrieval ingestion** | 3 calls in the SDK you already use: create store, upload file, `upload_and_poll`. | Not in `google-genai` at all — corpus + file management lives in `google-cloud-aiplatform` (**a new dependency**) or raw REST. |
| **Retrieval setup burden** | None beyond the above. | A **project-level** Serverless-vs-Spanner decision (`ragEngineConfig`) before the first corpus exists; `us-central1`/`us-east1`/`us-east4` refuse Spanner for new projects on capacity grounds. |
| **Citations** | Existing shape. | Grounding metadata, not `FileCitation` — the harness maps `text_annotation` off the latter today, so citation handling is new code. |
| **Harness changes needed** | **None.** The vendored plugin already forwards `FileSearchTool`. | None for the loop; retrieval is outside the harness either way. |
| **Observability** | ✅ Full vocabulary (below). | ❌ Degraded (below). |
| **Path maturity** | 3 examples + real tests exercise the OpenAI path. | **No example or test exercised `generate_content`** before this PR. |

## Observability: the measured difference

One turn, same agent, same tool. **OpenAI:**

```
turn_started, model_interaction_started, tool_requested, model_interaction_ended,
tool_start, tool_end, model_interaction_started, reply_delta ×10,
model_interaction_ended, reply, turn_end
```

**GEAP `generate_content`:**

```
turn_started, tool_start, tool_end, reply, turn_end
```

No `model_interaction_*`, no token accounting, no `tool_requested`, no `reply_delta`. The harness's
Gemini `generate_content` activities publish nothing (non-streamed) or only `reply_delta`
(streamed). That is the same coupling fixed for OpenAI in issue #50 / PR #63, and it wants the same
fix — publish at the model-invocation boundary — but on GEAP it's work you'd have to do, whereas on
OpenAI it's already done.

## The GEAP retrieval constraints worth reading before choosing

Each cost a failed attempt to find; all are documented in `rag_engine_probe.py`.

1. **Region mismatch silently returns no retrieval.** A client at `location="global"` querying a
   `us-west1` corpus produced a fluent answer from parametric knowledge, said it had no such
   information, and **raised nothing**. A nonexistent datastore behaves the same way. This is the
   sharpest edge in the path: misconfiguration doesn't fail, it quietly stops retrieving — so
   anything built here needs a liveness assertion, not just error handling.
2. **Corpora are regional; `global` is not a RAG location.**
3. **Model availability is regional, and the corpus's region constrains the model.**
   `gemini-3.5-flash` serves at `global` but 404s in `us-west1`; `gemini-2.5-flash` works there. So
   choosing RAG can force you onto an older model.
4. **Serverless vs Spanner is a project-level infrastructure decision**, and corpus creation is a
   long-running operation whose first `ragCorpora.list` can lag.

By contrast, the OpenAI retrieval path produced no surprises: create, upload, poll, query.

## Recommendation

**Go OpenAI**, unless something outside this analysis (existing Google commitments, data residency,
pricing) outweighs it. The reasoning, in order of weight:

1. **Retrieval is a drop-in.** It's the agent's core capability, it's proven, and it needed zero
   harness changes.
2. **You keep full observability** — model spans and token accounting — instead of having to
   rebuild it on the Gemini path first.
3. **It's the mature path in this harness.** Three examples and real tests, versus a
   `generate_content` path that had no coverage until this PR.
4. **GEAP's retrieval carries a silent-failure mode.** For a doc-QA agent, retrieval that quietly
   stops working while still answering fluently is close to the worst possible failure.

**Do not wait for Interactions-on-GEAP.** Google's forum thread has users still asking as of
2026-07-19 after a Jan-2026 "couple of months" estimate. Waiting is only rational if your deadline
is far enough out to absorb an unbounded slip, which the legal framing suggests it isn't.

### One hedge worth considering either way

If retrieval must be **approval-gated or fully observable**, neither hosted option gives you that:
`file_search` runs server-side, never passes through `run_tool`, and so emits no `tool_start` /
`tool_end` and cannot be gated (harness spec §11 defers hosted tool spans — asserted in the OpenAI
example's test). The alternative is retrieval as an ordinary harness tool —
`@agent.activity_tool_defn async def search_docs(query)` over any index — which costs more code but
is **provider-agnostic**, so it decouples the retrieval decision from the backend decision entirely.
Given the deadline pressure, that's the option that preserves the most freedom.

## Artifacts

- [`examples/hello_gemini_enterprise`](../../examples/hello_gemini_enterprise) — the same agent on
  both Gemini surfaces (Interactions, refused by GEAP; `generate_content`, works), plus
  `rag_engine_probe.py`.
- [`examples/hello_openai_file_search`](../../examples/hello_openai_file_search) — doc-QA on
  OpenAI's hosted `file_search`, with the observability and tool-lifecycle contrast asserted.
