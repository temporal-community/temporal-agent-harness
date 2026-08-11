# Monty evals — demo runbook

A ~12 minute live demo: traces of a real agent, then a dataset run that scores it. Followed by a
code walkthrough.

The pitch in one line: **the agent's code never changes.** Everything below is bolted on from
outside.

---

## Before the call (do this ahead of time — 15 min)

Model calls and simulated activity latency make a full dataset run take minutes. Do not let a
Zoom call watch that.

1. **Langfuse.** Use [Langfuse Cloud](https://cloud.langfuse.com) — free, 2 minutes. Self-hosting
   pulls a four-container compose stack (postgres + clickhouse + redis + minio) and is not worth
   it live. Create a project, copy the keys.

2. **`.env.local`** at the repo root:
   ```bash
   GEMINI_API_KEY=...
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com   # or https://us.cloud.langfuse.com
   ```

3. **Do a full rehearsal run**, and keep it. `just evals-seed && just evals-run baseline`. This
   gives you a finished experiment to show if the live run is slow, and it warms the dataset.

4. **Pick your one failing case** from the rehearsal and have its trace open in a tab. A demo
   that only shows green teaches nothing; the failure is the interesting part.

5. Four terminals, cwd `examples/monty`: `temporal` · `session-manager` · `worker` · `server`.
   Browser tabs: Langfuse, `localhost:8000` (agent UI), `localhost:8233` (Temporal UI).

6. **Only if you plan to do Act 4** (the cross-SDK bit): `OPENAI_API_KEY` set, plus a fifth
   terminal for `just worker-openai-hello`, and rehearse `just evals-seed-openai &&
   just evals-run-openai baseline` too.

---

## Act 1 — traces, with no eval code at all (4 min)

The point: you get the observability half before writing a single test case.

1. Start the worker and **read the banner out loud**:
   ```
   Monty dynamic agent worker ready: ... tracing=ON -> https://cloud.langfuse.com
   ```
   Then say: *that line is the entire setup.* Tracing is enabled in the **worker**, because that
   is where the workflow and its activities run.

2. In the agent UI, chat: *"Book me the cheapest flight from SFO to JFK on 2026-07-01 for Ada
   Lovelace."*

3. Switch to Langfuse while it works. One trace per turn. Expand it and walk the tree top-down:

   ```
   agent.turn                          input = the user's message, output = the typed reply
   ├── chat gemini-3.5-flash           gen_ai.usage.* → cost, computed automatically
   ├── execute_tool search_flights     input = the model's arguments
   ├── execute_tool book_flight
   └── chat gemini-3.5-flash
   ```

   **The three things to point at:**
   - **Cost is populated** and nobody wrote cost code — the spans carry OpenTelemetry's `gen_ai`
     semantic conventions, so Langfuse computes it.
   - **`temporal.attempt`** on a model span. If it is >0, that model call *was retried* and
     succeeded. No non-durable framework can show you that, because in one there is no retry —
     there is just a failed request.
   - The trace spans **two processes**. `agent.turn` was created in the workflow; `chat …` was
     created inside an activity, potentially on another machine. Nothing in the harness
     correlates them — Temporal's OTel plugin carries the span through its own headers.

4. **The human-in-the-loop moment** (optional, high impact). Run `/approvals strict` in the chat,
   ask it to book again, and *wait* before approving. Then show the `agent.tool_approval` span:
   its duration is how long the agent waited on you. That is the number nobody else can produce,
   and it is why the tool span opens *before* the approval gate rather than after.

---

## Act 2 — the dataset run (5 min)

```bash
just evals-seed
```
Show the dataset in Langfuse: 10 cases, 5 of them multi-turn.

Open `dataset.py` on screen for ten seconds and make one point: **a case is a conversation, not a
prompt.**

```python
"change-mind-before-booking": _case(
    TurnStep.text("Find me flights from SFO to JFK on 2026-07-01."),
    TurnStep.text("Actually, make it 2026-07-02 instead."),
    TurnStep.text("Book the cheapest one for Ada Lovelace."),
    expected={"books_flight": True, "date": "2026-07-02"},
),
```
*Most eval tools cannot express this, because they have no durable session to talk to. Here the
session is a workflow, so a case is just three messages.*

Then run one case live (fast enough to watch):
```bash
just evals-case demo book-cheapest-sfo-jfk
```

And show the full rehearsal run's output for the aggregate:
```
baseline: 7/10 cases passed
  booked_exactly_once                100%
  booked_the_cheapest_flight          70%
  FAIL change-mind-before-booking: last search used date '2026-07-01', expected '2026-07-02'
```

**Land the failure.** Open that case's trace in Langfuse and show the two `search_flights` calls —
you can see the agent searched the old date. The score didn't just say "wrong", it said which
tool call was wrong, and the trace shows why.

---

## Act 3 — why the scores are trustworthy (3 min)

This is the part that separates it from a vibe check.

Show `evaluators.py`, one function:

```python
def booked_exactly_once(script, result) -> list[Score]:
    calls = [e for e in result.events_of_type("tool_start") if e.tool_name == "book_flight"]
    return [Score.boolean("booked_exactly_once", len(calls) == 1, ...)]
```

*This scores what the agent **did**, not what it said. Every tool call is on the harness's event
stream, so the evaluator can count them.* Then the case that motivates it:

> `no-double-booking-on-followup` — the user asks *"what's my confirmation code again?"* and the
> agent books a second flight. The reply looks perfect. The user is charged twice. **Only a call
> count catches that.**

Then the strongest one:

```python
expected_code = make_booking_ref("AIR", booking["flight_id"], booking["passenger_name"])
```

*Monty's simulated backend is deterministic — seeded off the request. So the world is fixed while
the agent is free, and the confirmation code the booking should have produced is computable. If
the agent states a different, perfectly well-formed code, we catch it. A "looks like a code"
regex would wave it through.*

Close with: **these evaluators are unit-tested.** `pytest tests/examples/monty/test_evaluators.py`
— 19 tests, 0.2 seconds, no API key, no network. A scorer you cannot test is a scorer you cannot
trust, and a broken one silently invalidates every number it produces.

---

## Act 4 (optional, 2 min) — the same thing on a different AI SDK

The strongest structural point in the whole demo, and it costs two minutes.

`examples/openai_hello` is the OpenAI Agents SDK example: a one-tool weather assistant, nothing
to do with travel booking or Code Mode or Gemini. It has its own dataset and scorers in
`examples/openai_hello/evals/`.

```bash
just worker-openai-hello          # separate terminal; needs OPENAI_API_KEY
just evals-seed-openai
just evals-run-openai v1
```

Show one of its traces next to a Monty trace. **Same span names, same shape, same attributes.**
Then say:

> The turn, tool and approval spans come from the harness runner and the tool dispatchers, so
> they are identical for every SDK. The dataset format and the scorers are the same too — the
> only thing that changed is `workflow_type`. Gemini here is the interesting case *because* it
> has no OpenTelemetry integration of its own and still produces a full trace.

If someone asks about prompts and completions: the OpenAI plugin has a `use_otel_instrumentation`
flag that turns on the Agents SDK's own OpenInference instrumentation, and those spans nest under
ours automatically. Set `OPENAI_HELLO_SDK_TRACING=1` (needs
`openinference-instrumentation-openai-agents` installed). The harness is told it is on so it
stops reporting the same tokens and the backend does not bill them twice.

**Do not switch this on live unless you rehearsed it** — it needs a package that is not a harness
dependency, and it fails at worker startup if missing.

---

## Code walkthrough (10 min, screen-share)

Five files, in this order. The narrative is *"we tried to build a lot and Temporal had already
done most of it."*

### 1. `examples/monty/conversational_workflow.py` — 20 seconds
Scroll it. Say nothing except: **there is no tracing code in here, and there never will be.** Set
the baseline before showing anything else.

### 2. `temporal_agent_harness/harness/tracing.py` — the design call
Read the module docstring aloud; it is written for exactly this. The point:

> The original design was an out-of-band *projector* that rebuilt a trace tree from the AgentEvent
> stream, with hash-derived deterministic trace ids, to solve cross-process and cross-turn
> correlation. Then we found `temporalio.contrib.opentelemetry`'s `OpenTelemetryPlugin`, which
> already gives replay-safe spans **inside workflow code** and propagates the active span into
> activities and child workflows. Both problems, already solved. The projector was deleted before
> it was written.

Then show `record_error` and read its comment. This is the best story in the codebase:

> Calling `span.record_exception()` in workflow code makes Temporal's replay-safe span **drop the
> entire span** — it assumes the workflow task is about to fail and retry, and does not want a
> span per attempt. But the harness *catches* handler errors on purpose to keep the session
> alive. So the task succeeds and the trace silently vanishes. **Every failed turn would have had
> no trace.** A test caught it.

### 3. `harness/agent_workflow.py` — the turn loop (search `tracing.turn_span`)
~15 lines. One `with` around the existing try/except/finally. Point out
`TurnStarted(otel_trace_id=span.trace_id)`: *this is the join between the OTel world and the
durable event stream — it is how a score finds its trace later.*

Then jump to the tool dispatcher and note the span opens **before** `_apply_approval_policy`, so
human think-time lands inside the tool span instead of falling into a gap.

### 4. `examples/monty/evals/{dataset,evaluators}.py` — 2 min
Already covered in Acts 2–3. Just re-show the multi-turn case and one evaluator.

### 5. `temporal_agent_harness/evals/langfuse/_experiment.py` — the closing point
It is short. Say why:

> **Nothing in here creates a span.** The traces already exist, shipped over plain OTLP by the
> harness itself. The Langfuse SDK is left with only what OpenTelemetry has no vocabulary for:
> datasets, experiments, scores. That is what keeps the harness vendor-neutral — point the
> exporter somewhere else and the traces follow.

---

## If someone asks

**"What if the worker crashes mid-turn?"** — The turn resumes; that's Temporal. But **that turn's
trace is lost**: the replay-safe span suppresses export when `end()` lands during a replay. It's
bounded for short turns, but a turn parked for hours on a human approval is exactly the harness's
signature feature. It's a known limitation, written down, not yet solved.

**"Have you run this at scale?"** — No. It's a prototype. Ten cases, one agent, one provider.

**"Why not use Langfuse's own experiment runner?"** — For a normal app you could. The reason we
run the loop is that a case here is a multi-turn conversation against a durable session, and the
scores read the tool-call stream. Neither fits a single-shot task function.

**"Does this work with OpenAI Agents / Pydantic AI?"** — Yes, and Act 4 shows it running. The
turn/tool/approval spans are at the harness layer, so they're identical for every SDK; model
spans are wired for all three. Gemini is the headline case precisely *because* it has no OTel
integration of its own and gets a full trace anyway. One gotcha worth knowing if you improvise:
`OpenAIAgentsPlugin` supplies its own payload converter and rejects a foreign one, so an OpenAI
worker must not also pass `data_converter=`.

**"Double-counted cost if I also turn on my SDK's instrumentation?"** — No. Tell the harness and
it stops claiming those tokens under the semantic-convention keys. Scoped per SDK, because one
worker can host several. `tests/harness/test_tier2_instrumentation.py`.
