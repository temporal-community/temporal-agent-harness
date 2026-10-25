# Eval Swarm

Evaluate a harness agent through real conversations, grade it against its tool calls, and keep
the evidence for repeatable comparisons. The default target is
[Monty's conversational travel agent](../monty/README.md); a run can specify another target and
its evaluation profile. Defaults are deliberately small: 16 cases, at most two conversations,
and one judge. Each category lane runs sequentially.

The dispatcher starts case writers, a shared judge, and a lead for each category as capacity
becomes available. Each lead starts a fresh tester and target for each case. The board uses fresh
lanes per run so unfinished work from an earlier run cannot contaminate the next one.

For the first idea-to-product milestone, see [INCUBATION.md](INCUBATION.md). It includes a candidate
hypothesis, source-pinned evidence export, product-fit assessment, explicit readiness declaration,
and a command that prepares a promotion patch into `/products`.

## How a case runs

1. A **writer** invents the case: a persona, a goal (including details the user holds back until
   asked), an opening message, and success criteria a judge can check.
2. A **lead** claims it from its category's lane and starts a **tester** for it.
3. The tester starts a **fresh target** as its subagent, sets its model, and holds the
   conversation for up to `max_turns` user messages, playing the user with a model of its own.
4. The tester reads the target's **tool calls** off the target's own event stream: every script
   it ran, every search and booking, with inputs and results.
5. It asks a **judge** — a peer it did not start — for a verdict. The judge grades against the
   category's rubric with the tool calls as ground truth: a flight, price or confirmation code
   the agent states must come from a tool output.
6. The lead completes the board item with the verdict, and the dispatcher's report aggregates
   them: pass rate per category, failure modes, and the worst failures with the workflow id of
   the target that failed, so you can open its session and see exactly what it did.

## Reading results, and running the same cases again

The dispatcher session supports these messages:

| Send | You get |
| --- | --- |
| `evaluate` | A run. Its reply is the report: pass rates per category, failure modes, the worst failures, and — when it reran a saved suite — what was fixed and what regressed since that suite's previous run. |
| `report` | The latest run (or `run_id`) with every failure's evidence: the goal, the unmet criteria with the judge's reasons, and the tools the agent called. Regressions first. |
| `case` | One case in full (`case_id`, e.g. `budget-003`): the verdict per criterion, the whole conversation, and every tool call with its input and result. |
| `export` | The full run, cases, untruncated tool evidence, target, profile and configuration as JSON. |
| `suites` | The saved suites and recent runs, from every session. |

Every run saves the cases it used as an immutable **suite**, named after the run (`<session id>#<n>`) or
after `save_as`. Send `evaluate` with `suite` set to one, and the run reuses those exact cases
instead of writing new ones. Suite names cannot be overwritten with different cases. Comparisons
require the same case content, target/profile, turn budget and evaluator models; model-driven users
and judges still introduce variance between runs.
That is the loop: run, read the failures, change the agent, rerun the suite, read what moved.
Suites and run records live in an `EvalLibrary` workflow per agent type
(`eval-library-<type>`); a case's evidence stays with the session that ran it.

The eight categories — flight search, booking a flight, booking a hotel, a whole trip, missing
details, a change of plans, a budget limit, and out-of-scope requests — are in
[`categories.py`](categories.py), each with the brief its writers follow and the rubric its
judges apply.

## Two providers

The target runs on **Gemini** (`gemini-3.1-flash-lite` by default, set on each instance), and
the swarm's own agents on **OpenAI**: small models for writing cases and playing users, a
stronger one for judging. The default uses a different provider for the judge and target; a custom target may use any
provider supported by its worker. Every model is a field of `EvalRun.models`;
check the defaults against what your accounts offer and what they cost.

**Cost and rate limits.** A run is roughly `cases × max_turns` target turns (each one or two
model calls) plus as many tester calls, a judge call per case, and one writer call per ten cases.
`concurrency` caps the conversations in flight, which is what bounds the request rate: lower it
if your API tier throttles, at the price of a longer run.

## Run

The dev server needs the swarm search attributes, which the root `just temporal` registers (this
directory's `just temporal` runs it). Then, from this directory:

```bash
just temporal          # 1. local Temporal dev server, with the swarm search attributes
just session-manager   # 2. session-manager worker
just server            # 3. harness API + console on http://localhost:8000
just worker            # 4. the eval agents, and the agent under test
```

or steps 2–4 together with `just dev`. The worker runs the agents under test in a child process
of its own: they run their scripts inside their workflow tasks, and sharing one Python process
with the swarm slowed everyone's workflow tasks. A live run needs `OPENAI_API_KEY` and `GEMINI_API_KEY` in
the repo-root `.env.local`. In the console, start an **Eval Swarm** session and send `evaluate`.
Open the **Swarm** pane to watch it: the testers and their targets fill each category's tier,
judges light up as testers ask them, and each lane drains as cases finish.

**No keys?** Send `evaluate` with `simulate: true` (or run `just worker-simulated`): the writers,
users and judges are scripted, and the target is a scripted travel agent. It costs nothing and
exercises everything except the models.

## Files

| File | Role |
| --- | --- |
| `eval_agents.py` | The dispatcher, writers, leads, testers and judges, and the report. |
| `categories.py` | What is tested: each category's brief and rubric. |
| `models.py` | The messages, init data and results. |
| `trace.py` | The activity that reads a target's tool calls off its stream for the judge. |
| `library.py` | Saved suites and run records, and the comparison between runs of a suite. |
| `reports.py` | The report, and the markdown pages `report`, `case` and `suites` reply with. |
| `simulated.py` | Scripted stand-ins for every model, and the scripted target. |
| `progress.py` | The dispatcher's progress, as observable state. |
| `worker.py` | Two workers: the eval agents (OpenAI), and the target (Gemini) in a child process. |
