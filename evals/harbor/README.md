# Agent Kit evals with Harbor

Experimental integration for running [Harbor](https://github.com/harbor-framework/harbor)
evals against Temporal Agent Kit agents.

## Adapter and ATIF converter

[`adapter.py`](src/harness_evals/adapter.py) implements Harbor's `BaseAgent`.
It creates a fresh session on an existing Temporal worker, submits the task
instruction, records the events and final response, and closes the session.
It also handles timeouts and saves partial recordings when a run fails.

[`atif.py`](src/harness_evals/atif.py) converts the recording to Harbor's
Agent Trajectory Interchange Format (ATIF). It groups model interactions,
tool calls and results, and replies into a trajectory that Harbor can display
and verifiers can inspect. Both formats are saved:

- `agent/run.json`: native events and final response.
- `agent/trajectory.json`: ATIF trajectory.

Harbor runs the task's `tests/test.sh` in a Docker container with those files
available at `/logs/agent/`. The example tasks use Rewardkit for deterministic
checks and optional LLM judging, writing `reward.json` and `reward-details.json`
under `/logs/verifier/`.

## Use your own agent

Edit [`run.py`](run.py) with your workflow type, task queue, Temporal address,
message handler, and instruction input field. Start your worker separately.
The same adapter works with either bundled SDK example; users do not need a
new adapter for each agent.

Copy a task from [`tasks/`](tasks/) and define its instruction, Docker environment,
and grading criteria. The weather task checks tool use and answer quality;
the greeting task checks that the agent answers without calling tools.

## Try it

Requires Python 3.12+ and `uv`. From the repository root:

```sh
cd evals/harbor
uv sync --locked
uv run harbor view examples/saved-jobs --port 8080 --no-build
```

Open <http://127.0.0.1:8080> and select `rewardkit-matrix` to inspect four saved
trials. These recordings were captured with Agent Harness 0.8.0. Viewing them
requires no Temporal server, Docker, or model credentials.

For a new run, have Docker running and `OPENAI_API_KEY` set in the worker's
terminal. From this directory, in separate terminals:

```sh
# 1. Start Temporal, unless a local server is already running.
temporal server start-dev --ip 127.0.0.1 --port 7233 --headless

# 2. Start this checkout's OpenAI Hello worker.
uv run harness-evals demo-worker openai

# 3. Run the task using the configuration in run.py.
uv run python run.py
```

Results are saved under `jobs/`. To include LLM judging, set `OPENAI_API_KEY`
in the runner's terminal too, then use:

```sh
uv run harness-evals run --agent openai --task tasks/weather --judge
```

To rescore a saved recording without running the agent:

```sh
uv run harness-evals rescore \
  examples/saved-jobs/rewardkit-matrix/weather__sxfq4Wh/agent/run.json \
  --task tasks/weather --output jobs/rescore-weather
```

Rescoring runs the same task script on the host. Add `--judge` for LLM judging.
Each rescore needs a new output directory.

## Scope

Local connections, one agent, and one text instruction per trial. Agent tools
run on the external worker; sandbox tool access is not wired up. Subagents,
approvals, callbacks, and Cloud/TLS configuration are not supported yet.

The project has its own dependencies and uses the SDK from `../../python`.
See [`VALIDATION.md`](VALIDATION.md) for checks and the demo-only OpenAI
compatibility workaround. Regression tests are in [`tests/`](tests/).
