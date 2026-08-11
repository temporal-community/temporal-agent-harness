# Evals demo — OpenAI Hello agent

A ~12 minute live demo: traces of a real agent, then a dataset run that scores it. Followed by a
code walkthrough.

The pitch in one line: **the agent's code never changes.** Everything below is bolted on from
outside.

Everything here needs only `OPENAI_API_KEY` and Langfuse keys.

---

## Before the call (do this ahead of time — 15 min)

Model calls take seconds and a full dataset run takes minutes. Do not let a Zoom call watch that.

1. **Langfuse.** Use [Langfuse Cloud](https://cloud.langfuse.com) — free, 2 minutes. Self-hosting
   pulls a four-container compose stack (postgres + clickhouse + redis + minio) and is not worth
   it live. Create a project, copy the keys.

2. **`.env.local`** at the repo root:
   ```bash
   OPENAI_API_KEY=sk-...
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=https://cloud.langfuse.com   # or https://us.cloud.langfuse.com
   ```

3. **Do a full rehearsal run and keep it:**
   ```bash
   just evals-seed-openai
   just evals-run-openai baseline
   ```
   This gives you a finished experiment to fall back on if the live run drags, and warms the
   dataset.

4. **Pick your one failing case** from the rehearsal and have its trace open in a tab. A demo
   that only shows green teaches nothing; the failure is the interesting part. If everything
   passed, force one: temporarily add a case expecting `weather_cities: []` for a question that
   obviously needs a lookup.

5. **Four terminals**, repo root:
   ```bash
   just temporal              # local dev server
   just session-manager       # packaged session-manager worker
   just server                # UI + /api on :8000 (merged registry)
   just worker-openai-hello   # the agent
   ```
   Browser tabs: Langfuse, `localhost:8000` (agent UI), `localhost:8233` (Temporal UI).

   > **Gotcha:** the session manager caches its registry on first start. If you previously ran a
   > different example's server, run `just reset-manager` before `just server`, or start a fresh
   > Temporal dev server. Other agents in the merged registry will be listed but have no worker
   > running — pick **OpenAI Hello**.

---

## Act 1 — traces, with no eval code at all (4 min)

The point: you get the observability half before writing a single test case.

1. Start the worker and **read the banner out loud**:
   ```
   OpenAI hello agent worker ready: ... tracing=ON -> https://cloud.langfuse.com
   ```
   Then say: *that line is the entire setup.* Tracing is enabled in the **worker**, because that
   is where the workflow and its activities run.

2. In the agent UI, pick **OpenAI Hello** and ask: *"What's the weather in Tokyo?"*

3. Switch to Langfuse. One trace per turn. Expand it and walk the tree top-down:

   ```
   agent.turn                     input = the user's message, output = the typed reply
   ├── chat gpt-5.1               first model call — decides to use the tool
   ├── execute_tool get_weather   input = the model's arguments, output = the tool's result
   └── chat gpt-5.1               second model call — writes the answer
   ```

   **The three things to point at:**
   - **Cost is populated** and nobody wrote cost code — the spans carry OpenTelemetry's `gen_ai`
     semantic conventions, so Langfuse computes it from `gen_ai.usage.*`.
   - **`temporal.attempt`** on a model span. If it is >0, that model call *was retried* and then
     succeeded. No non-durable framework can show you that, because in one there is no retry —
     there is just a failed request.
   - The trace spans **two processes**. `agent.turn` and `execute_tool` were created in the
     workflow; both `chat` spans were created inside activities, potentially on another machine.
     Nothing in the harness correlates them — Temporal's OTel plugin carries the active span
     through its own headers.

4. **The human-in-the-loop moment** (optional, highest impact in the demo). In the chat, run:
   ```
   /approvals strict
   ```
   Ask about the weather again, and **wait** before approving. Then show the
   `agent.tool_approval` span: its duration is how long the agent sat waiting for you. That is
   the number nobody else can produce, and it is why the tool span opens *before* the approval
   gate rather than after — so the wait is charged to the tool call instead of vanishing into a
   gap between spans.

---

## Act 2 — the dataset run (5 min)

```bash
just evals-seed-openai
```
Show the dataset in Langfuse: 7 cases, 2 of them multi-turn.

Put `examples/openai_hello/evals/dataset.py` on screen for ten seconds and make one point: **a
case is a conversation, not a prompt.**

```python
"followup-carries-the-question": _case(
    TurnStep.text("What's the weather in Tokyo?"),
    TurnStep.text("What about Paris?"),
    expected={"weather_cities": ["Tokyo", "Paris"]},
),
```
*"What about Paris?" only means "the weather in Paris" if the agent kept the thread. Most eval
tools cannot express this, because they have no durable session to talk to. Here the session is
a workflow, so a case is just two messages.*

Then run one case live (fast enough to watch):
```bash
just evals-run-openai demo --case followup-carries-the-question
```
*(from `examples/openai_hello`: `just evals-case demo followup-carries-the-question`)*

And show the rehearsal run's output for the aggregate. The format looks like this — **the
numbers below are illustrative, not measured; use your own rehearsal output**:
```
baseline: 5/7 cases passed
  called_the_tool_for_the_right_cities            71%
  did_not_look_up_the_same_city_twice            100%
  did_not_use_a_tool_it_did_not_need              67%
  reply_states_the_temperature_the_tool_returned 100%
  FAIL chitchat-uses-no-tool: called get_weather for ['tokyo'] on a question that needed no lookup
```

**Land the failure.** Open that case's trace in Langfuse and show the `execute_tool get_weather`
span that shouldn't be there. The score didn't just say "wrong" — it said which tool call was
wrong, and the trace shows it happening.

---

## Act 3 — why the scores are trustworthy (3 min)

This is what separates it from a vibe check.

Show `evaluators.py`, one function:

```python
def called_the_tool_for_the_right_cities(script, result) -> list[Score]:
    want = _norm(script.expected["weather_cities"])
    got  = _norm([e.tool_input["city"]
                  for e in result.events_of_type(AgentEventType.TOOL_START)
                  if e.tool_name == "get_weather"])
    ...
```

*This scores what the agent **did**, not what it said. Every tool call is on the harness's event
stream, so the evaluator can read the arguments the model actually passed.*

Two cases that make the point:

> **`chitchat-uses-no-tool`** — the user says hello. A one-tool agent that reaches for its tool
> at everything looks completely fine in a demo and wastes a round trip on every unrelated
> message. Only the call stream reveals it.

> **`followup-about-the-same-answer`** — "is that warm enough for shorts?" needs no second
> lookup. Re-calling the tool produces a *correct answer* and a wasted call. The output is
> indistinguishable; the call stream is not.

Then the strongest one — anti-hallucination:

```python
allowed = temperatures found in the tool's own output
stated  = temperatures found in the reply
invented = stated - allowed
```

*The tool returns a canned, deterministic string, so the only defensible number is the one it
returned. If the agent says 68°F when the tool said 72°F, we catch it. A "does the reply mention
a temperature?" check would wave that straight through.*

Close with: **these evaluators are unit-tested.**
```bash
uv run pytest tests/examples/openai_hello/test_evaluators.py
```
14 tests, under a second, no API key, no network — they run against synthetic events. A scorer you
cannot test is a scorer you cannot trust, and a broken one silently invalidates every number it
produces.

---

## Act 4 (optional) — prompts and completions

Only if you rehearsed it: the harness's spans deliberately do **not** carry prompt text, because
putting a whole conversation on the durable event stream on every model call is the wrong cost
model. The Agents SDK's own instrumentation captures exactly that, and because Temporal's plugin
has already made our model span current inside the activity, its spans nest underneath ours with
no correlation scheme at all.

```bash
pip install openinference-instrumentation-openai-agents   # NOT a harness dependency
OPENAI_HELLO_SDK_TRACING=1 just worker-openai-hello
```

Point out that the harness is *told* the SDK is instrumented, so it stops reporting the same
tokens under `gen_ai.usage.*` and the backend does not bill them twice.

**Skip this if you did not rehearse it** — the worker exits at startup if the package is missing.

---

## Code walkthrough (10 min, screen-share)

Five files, in this order. The narrative is *"we tried to build a lot and Temporal had already
done most of it."*

### 1. `examples/openai_hello/workflow.py` — 20 seconds
Scroll it. Say nothing except: **there is no tracing code in here, and there never will be.** Set
the baseline before showing anything else. It's ~90 lines: a system prompt, one tool, and an
`ask` handler that runs the SDK's loop.

### 2. `temporal_agent_harness/harness/tracing.py` — the design call
Read the module docstring aloud; it is written for exactly this. The point:

> The original design was an out-of-band *projector* that rebuilt a trace tree from the harness's
> AgentEvent stream, with hash-derived deterministic trace ids, to solve cross-process and
> cross-turn correlation. Then we found `temporalio.contrib.opentelemetry`'s
> `OpenTelemetryPlugin`, which already gives replay-safe spans **inside workflow code** and
> propagates the active span into activities and child workflows. Both problems, already solved.
> The projector was deleted before it was written.

Then show `record_error` and read its comment. This is the best story in the codebase:

> Calling `span.record_exception()` in workflow code makes Temporal's replay-safe span **drop the
> entire span** — it assumes the workflow task is about to fail and retry, and does not want one
> span per attempt. But the harness *catches* handler errors on purpose to keep the session
> alive. So the task succeeds and the trace silently vanishes. **Every failed turn would have had
> no trace.** A test caught it.

### 3. `harness/agent_workflow.py` — the turn loop (search `tracing.turn_span`)
~15 lines: one `with` around the existing try/except/finally. Point out
`TurnStarted(otel_trace_id=span.trace_id)` — *this is the join between the OTel world and the
durable event stream; it is how a score finds its trace later.*

Then jump to the tool dispatcher and note the span opens **before** `_apply_approval_policy`, so
human think-time lands inside the tool span (the Act 1 approval moment).

### 4. `examples/openai_hello/evals/{dataset,evaluators}.py` — 2 min
Already covered in Acts 2–3. Just re-show the multi-turn case and one evaluator.

### 5. `temporal_agent_harness/evals/langfuse/_experiment.py` — the closing point
It is short. Say why:

> **Nothing in here creates a span.** The traces already exist, shipped over plain OTLP by the
> harness itself. The Langfuse SDK is left with only what OpenTelemetry has no vocabulary for:
> datasets, experiments, scores. That is what keeps the harness vendor-neutral — point the
> exporter somewhere else and the traces follow.

---

## If someone asks

**"Does this only work with the OpenAI Agents SDK?"** — No. The turn, tool, approval, callback
and subagent spans come from the harness runner and the tool dispatchers, so they are identical
for every SDK; model spans are wired for OpenAI Agents, Gemini and Pydantic AI. There is a second
dataset and scorer set for the Gemini example in `examples/monty/evals/`, built to exactly the
same shape. *Being straight about it: today's demo only exercises the OpenAI path.*

**"What if the worker crashes mid-turn?"** — The turn resumes; that's Temporal. But **that turn's
trace is lost**: the replay-safe span suppresses export when `end()` lands during a replay. It's
bounded for short turns, but a turn parked for hours on a human approval is exactly the harness's
signature feature. Known limitation, written down, not yet solved.

**"Have you run this at scale?"** — No. It's a prototype: 7 cases, one agent, one provider.

**"Why not use Langfuse's own experiment runner?"** — For a normal app you could. The reason we
run the loop is that a case here is a multi-turn conversation against a durable session, and the
scorers read the tool-call stream. Neither fits a single-shot task function.

**"Double-counted cost if I turn on my SDK's instrumentation?"** — No. Tell the harness and it
stops claiming those tokens under the semantic-convention keys. Scoped per SDK, because one
worker can host several. See `tests/harness/test_tier2_instrumentation.py`.

**"Any gotchas building an OpenAI worker?"** — One: `OpenAIAgentsPlugin` supplies its own payload
converter and rejects a foreign one, so don't also pass `data_converter=` (most harness workers
do). And `setup_tracing()` must run *before* the plugin is constructed, because
`use_otel_instrumentation=True` validates the provider at construction time. Both pinned in
`tests/ai_sdks/openai_agents/test_plugin_composition.py`.
