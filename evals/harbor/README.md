# Agent Kit evaluations with Harbor

An experimental integration for evaluating Temporal Agent Kit agents through Harbor.
Everything for this prototype lives here: implementation, configuration, tasks,
tests, dependencies, documentation, and saved results. It uses the Python SDK and
example agent workers from this checkout through an editable dependency on `../../python`.
The checkout currently uses the `temporal_agent_harness` package name.

## Find the components

| File or directory | Responsibility | Who changes it? |
| --- | --- | --- |
| [`run.py`](run.py) | Agent connection/configuration and a programmatic Harbor run | The user |
| [`src/harness_evals/adapter.py`](src/harness_evals/adapter.py) | Fresh session, task submission, event capture, timeout, cleanup | Integration maintainers |
| [`src/harness_evals/atif.py`](src/harness_evals/atif.py) | Native events to Harbor's ATIF trajectory | Integration maintainers |
| [`src/harness_evals/cli.py`](src/harness_evals/cli.py) | Convenience run commands and saved-recording rescoring | Integration maintainers |
| [`tasks/`](tasks/) | Instructions, Docker environment, Rewardkit criteria and judge rubrics | The user |
| [`src/harness_evals/demo.py`](src/harness_evals/demo.py) and [`demo_entry.py`](src/harness_evals/demo_entry.py) | Local example-worker launcher and compatibility fix | Demo only |
| [`tests/`](tests/) | Adapter lifecycle, exporter, grading, and rescoring regression tests | Integration maintainers |
| [`examples/saved-jobs/rewardkit-matrix/`](examples/saved-jobs/rewardkit-matrix/) | Four previously recorded, graded example trials | Demo evidence |
| [`VALIDATION.md`](VALIDATION.md) | What has been verified and current limitations | Integration maintainers |

## How it works

A Harbor **task** contains an instruction, an environment, and a verifier. One
attempt is a **trial**; a **job** groups trials. Our adapter connects a trial to
an Agent Kit agent running on an external Temporal worker.

1. Start Temporal and the worker. The worker waits for sessions; it has not run an eval yet.
2. Harbor creates the task's Docker container and calls the adapter on the host.
3. The adapter creates a fresh agent session and submits the task instruction.
4. The adapter records native events in `agent/run.json` and exports `agent/trajectory.json` (ATIF).
5. Harbor runs the task's `tests/test.sh` in the container. It invokes Rewardkit,
   which writes scores and explanations. The adapter closes its session after capture.

Harbor bind-mounts each local trial's `agent/` directory at `/logs/agent/` in the
container, so the verifier can read the recordings. `/logs/verifier/` similarly
maps back to the trial's `verifier/` directory. The example tasks use shared
verification: the verifier runs in Harbor's task container.

The agent and its tools run on the external worker. This prototype does not route
tools into the task's sandbox filesystem. ATIF is an interoperability format,
not an evaluator; a verifier can also inspect the retained native recording.

## First look: saved results

Python 3.12+ and `uv` are required. Run these commands from this directory:

```sh
uv sync --locked
uv run harbor view examples/saved-jobs --port 8080 --no-build
```

Open <http://127.0.0.1:8080>, select `rewardkit-matrix`, then `weather__sxfq4Wh`.
Inspect the instruction, weather tool call, result, final answer, and scores.
Individual checks and judge reasoning are in `verifier/reward-details.json`.
Viewing needs no Temporal server, Docker, worker, or model credentials.

The saved trials were captured by the original standalone prototype against
Agent Harness 0.8.0; they are historical evidence, not runs against this checkout.
They exercise OpenAI Hello and Pydantic AI Hello, each on weather and greeting.
Only portable paths in Harbor metadata have been adjusted; recordings and rewards
are preserved. New runs are written automatically under `jobs/`, which is ignored.

## Run a new evaluation

You need Docker running, the Temporal CLI, and `OPENAI_API_KEY` available to the
worker. Use your normal credential mechanism; no credentials belong in task files.

From this directory, in separate terminals:

```sh
# Terminal 1: skip this if your local Temporal server is already running.
temporal server start-dev --ip 127.0.0.1 --port 7233 --headless

# Terminal 2: start the OpenAI Hello worker from ../../python/examples/openai_hello.
uv run harness-evals demo-worker openai

# Terminal 3: creates a fresh agent session and runs the weather task.
uv run python run.py
```

The worker launcher uses this checkout's original worker module, with a local
Temporal profile and the demo-only compatibility fix described in `VALIDATION.md`.
It does not download another copy of the repository. You can also use the repo's
normal `just`/Bazel worker recipes if those tools are installed.

The convenience CLI can run both tasks, with deterministic grading by default:

```sh
uv run harness-evals run --agent openai --task tasks/weather --task tasks/greeting
```

Add `--judge` to run the quality rubrics too. This passes the existing host
`OPENAI_API_KEY` to the verifier container using Harbor's environment-variable
substitution. The worker's model is configured by the worker; `--judge-model`
selects only the grading model (default `openai/gpt-5.1`).

For both SDKs, start `uv run harness-evals demo-worker pydantic` in another
terminal and pass `--agent openai --agent pydantic` to the run command.
To view new results, point `harbor view` at `jobs` instead of `examples/saved-jobs`.
Stop the example workers and any dev server you started when finished.

## Evaluate your own agent

Edit the `AGENT` configuration in [`run.py`](run.py). Set its workflow type,
task queue, connection address/namespace, message handler, and the input field
that receives the instruction. `HarnessOptions` in `adapter.py` also supports
init data and a session timeout. Start your worker separately and define your
own tasks by copying an example. You reuse the adapter without subclassing it.

`run.py` calls Harbor's Python API directly; no CLI subprocess is required.
The convenience CLI's `--agent` names select only the two bundled examples.

Each task keeps its grading code together:

```text
tasks/weather/
  instruction.md
  task.toml
  environment/Dockerfile       # Python + pinned Rewardkit
  tests/
    test.sh                   # the actual verifier entrypoint
    behavior/checks.py         # built-in ATIF checks and custom assertions
    behavior/reward.toml       # combine deterministic checks
    quality.toml              # optional LLM judge and rubric
    reward.toml               # combine behavior and quality
```

Weather checks completion, `get_weather` use, Paris arguments, and a successful
tool result. Greeting checks completion and no tool use. Optional judges check
the final answer's quality. No custom Harbor verifier class is needed.

## Regrade a recording

```sh
uv run harness-evals rescore \
  examples/saved-jobs/rewardkit-matrix/weather__sxfq4Wh/agent/run.json \
  --task tasks/weather --output jobs/rescore-weather
```

This executes the same task's `tests/test.sh` directly on the host with installed
Rewardkit. It needs neither Temporal nor Docker. Without `--judge` it makes no
model call. The output is a **new directory** containing `reward.json`,
`reward-details.json`, input metadata, and verifier stdout/stderr. Existing output
directories are refused and the original evidence stays unchanged.
This accepts our saved recordings, not arbitrary existing Temporal session IDs.

## Development

This experiment has its own lockfile and environment so Harbor does not become an
SDK dependency. `evals/BUILD.bazel` excludes it from Gazelle generation. Changes
to the SDK and example agents still follow the repository's normal build rules.

```sh
uv sync --locked
uv run ruff check src tests tasks run.py
uv run ruff format --check src tests tasks run.py
uv run pytest -q
```

Tests need no API key, Docker, or running Temporal server. Current runtime scope:
local connections, one agent and one text message per trial. Subagents, tool
approvals, callback tools, Cloud/TLS configuration, and sandbox tool transport
are not implemented. See `VALIDATION.md` for evidence and remaining boundaries.
