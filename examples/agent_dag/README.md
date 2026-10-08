# Agent DAG Studio

Describe a job in plain language and an agent writes a **flow**: a short Python script that wires
fresh agents into a DAG — who runs in parallel, who waits for whom, what each one is told and which
tools it gets. Press **Run** and the flow executes durably, each step a real subagent workflow,
drawn live as a graph.

```python
"""Weekend in Seattle. Shape: 3 scouts (parallel) -> planner -> judge"""
import asyncio

async def main():
    flights, hotels, weather = await asyncio.gather(          # parallel
        run_agent({"name": "Flight scout", "instructions": "You find flights...",
                   "prompt": "Find flights SFO to Seattle, Oct 16-18.",
                   "tools": ["search_flights"], "model": "gpt-6.1-sol"}),
        run_agent({"name": "Hotel scout", ..., "tools": ["search_hotels"]}),
        run_agent({"name": "Weather scout", ..., "tools": ["get_weather"]}),
    )
    plan = await run_agent({                                   # then in sequence
        "name": "Planner", "instructions": "You book trips...",
        "prompt": f"Book this trip.\n\nFlights: {flights['output']}\n\nHotel: {hotels['output']}",
        "tools": ["book_flight", "book_hotel", "get_trip_summary"],
    })
    verdict = await run_agent({"name": "Judge", "instructions": "You audit itineraries...",
                               "prompt": f"Check this itinerary:\n\n{plan['output']}"})
    return verdict["output"]

asyncio.run(main())
```

That is the shape of an orchestration like

```ts
const reviewers = parallel(agent("Security", {...}), agent("Repository", {...}));
const judge = agent("Judge", { model: model.High, instructions: "..." });
await sequence(reviewers, judge).run({ tools: [...] });
```

written as ordinary Python: `asyncio.gather` is `parallel`, awaiting in order is `sequence`, and
each `run_agent(...)` call is an `agent(...)` with its own name, instructions, tools and model. The
tools here are deliberately trivial — the Monty example's simulated flight and hotel activities
and the OpenAI hello example's canned `get_weather` — because the point is the orchestration.

## Two agents

**`DagBuilderAgent`** (`workflow.py`) is the session you talk to. It has two handlers:

- **`generate`** — an OpenAI Agents SDK turn that writes or revises the flow. The script lives in
  your browser, not in the workflow, so the model reaches it only through three **callback tools**
  (`editor_tools.py`): `read_code`, `write_code` and `edit_code`. The studio fulfils each call
  against its editor and draws it as it lands — a whole script typed in, or a targeted edit
  glowing where it changed. The model's prompt carries the exact contract a flow runs under, taken
  from the Code Mode tool itself, and before it replies it must call **`check_code`**, which
  type-checks the script against that contract without running it
  (`agent.code_mode_type_check`) and reports each error with its line. The model fixes what it
  reports and checks again until the script passes, so what you get back will start when you
  press Run. `check_code` reads the script through a fourth callback, `export_code`, that the
  model is never given.
- **`execute`** — runs a script in **Code Mode** over one host function, `run_agent`, with no
  model in the loop (like the Monty example's `run_script`). The script is statically
  type-checked against `run_agent`'s signature before it runs: a misspelled tool name is an error
  to fix, not a failed step.

**`DagStepAgent`** (`step_agent.py`) is every step. `run_agent` starts one as a subagent per call,
passing a `StepSpec` — name, system prompt, tool names, model — as its **init data**, sends it the
step's prompt, and stops it once it replies. The agent carries nothing of its own, so one flow's
instances can be a flight scout and a judge. It publishes its spec as the `profile` state, which
is how the studio labels each node.

```
 browser (ui/)                         DagBuilderAgent                     DagStepAgent × N
 ─────────────                         ───────────────                     ────────────────
 "plan a trip…" ── generate ─────────▶ OpenAI turn
 read/write/edit ◀── callback tools ── (edits the script in the page)
 ▶ Run ── execute(script) ───────────▶ Code Mode sandbox
                                         run_agent(...) ──start + init data──▶ ask(prompt)
 live graph ◀──────── merged stream (builder + every step) ──────────────────── tools, reply
```

## Files

| File | Role |
| --- | --- |
| `workflow.py` | `DagBuilderAgent`: `generate`, `execute`, the `run_agent` host function and `check_code`. |
| `step_agent.py` | `DagStepAgent`: configured entirely by its `StepSpec` init data. |
| `editor_tools.py` | The callback tools the builder reads and edits the script with. |
| `step_tools.py` | The step tools, reused from `examples/monty` and `examples/openai_hello`. |
| `models.py` | The messages, init data, state and host-function shapes. |
| `worker.py` | Hosts both agents on one queue. |
| `client_sdk/` | Both agents' TypeScript types, generated by `just codegen-client-sdk`. |
| `ui/` | The studio: a Svelte page with no build step (see below). |

## Run

Set `OPENAI_API_KEY` in the repo-root `.env.local` (`cp .env.example .env.local` first if you
haven't). Then, each in its own terminal from this directory:

```bash
just temporal          # 1. local Temporal dev server (skip if you bring your own)
just session-manager   # 2. session-manager worker
just server            # 3. harness API on http://localhost:8000
just worker            # 4. both agents
just studio            # 5. the studio on http://127.0.0.1:5173
```

Pick a starter prompt (or write your own), watch the agent write the flow, press **Run flow**, and
click any node for its instructions, prompt, tool calls and reply. Follow-ups like "add a budget
reviewer before the judge" arrive as targeted edits. If a run fails, the chat offers to send the
failure to the builder; nothing is sent until you confirm.

The generic console on http://localhost:8000 can drive the agent too (send `generate`, then
`execute`), but nothing there answers the editor tools, so `generate` waits for a client that
does.

## The studio (`ui/`)

`App.svelte` lists the sessions; `Studio.svelte` is one flow, and holds the whole integration: a
typed `AgentSession<DagBuilderAgent>` whose `callbackTools` implement the editor tools.
`CodeEditor.svelte` is the read-only, highlighted script; `RunPanel.svelte` draws a run;
`ChatPanel.svelte` is the conversation.

- **The script is rebuilt from the stream.** Every change to it is a `write_code` or `edit_code`
  call on the session's event stream, so the page replays them to get the current script. A
  reload, or a second browser, shows the same flow with nothing stored anywhere else.
- **The graph comes from the builder's own stream.** Each `run_agent` call is a step, carrying the
  spec it was given and the reply it returned. Steps whose calls overlapped in time were gathered,
  so they share a column. The step's subagent view adds its live detail — streamed text and tool
  calls — while the run is on; a stopped subagent's events are not readable after a reload, so a
  replayed run shows each step's spec and reply without them.
- **No build step.** As in the tic-tac-toe example, `svelte-sw.js` compiles each component in the
  browser, here with its styles injected. `just check-ui` (and CI) type-check the components
  against the generated `client_sdk/` types.
