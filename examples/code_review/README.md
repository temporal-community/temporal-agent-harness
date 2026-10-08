# Code review through multiple agent harnesses

One example, three agents, real LiteLLM calls:

| Agent | Inner harness | Job |
|---|---|---|
| Coordinator | OpenAI Agents | Launch reviewers, receive reports, ask clarifications, consolidate findings |
| `correctness_openai` | OpenAI Agents | Logic bugs, edge cases, regressions, missing tests |
| `security_pydantic` | Pydantic AI | Authorization, injection, validation, secrets, unsafe operations |

Both reviewers start through the existing `agent.subagent_toolset()` and run
concurrently. The parent sends guidance using `send_message_tool()`. Each reviewer
sends progress and its **full findings report** to `parent` using the same tool.
Its typed turn reply contains only `{review_id, revision, report_delivered}`.
The coordinator consumes `AgentMessageInbox`, validates the report's sender,
review ID, angle, harness, and revision, then consolidates those message reports.
It can ask one follow-up per reviewer; revised findings are also sent by message.
The workflow orchestrates the initial parallel review; the coordinator's model
decides whether a clarification is needed and writes the overview.

## Run locally

From the repository root, install the example SDKs and UI if needed:

```sh
uv sync --group examples --extra ui
```

Configure your LiteLLM gateway; no direct provider keys are needed:

```sh
cp examples/code_review/.env.example examples/code_review/.env.local
# Edit .env.local: set LITELLM_BASE_URL and LITELLM_MODEL; set a gateway key if required.
set -a
source examples/code_review/.env.local
set +a
```

Use a LiteLLM model that supports streaming, tool calls, and structured outputs.
OpenAI Agents uses `/chat/completions` with a JSON-schema response format for the
correctness report. Pydantic AI uses the same endpoint with its structured output
tool. All three agents share `LITELLM_MODEL` by default; optional per-agent overrides
let you compare models as well as harnesses. Gateway credentials stay in the
worker environment, not prompts, Workflow arguments, or Signals.

Start Temporal if it is not already running:

```sh
temporal server start-dev
```

In the configured terminal, start the whole example stack:

```sh
.venv/bin/python -m examples.code_review.dev
```

The launcher creates the local `code-review-demo` namespace if missing and starts
the two SDK workers, session manager, and UI. It makes no model calls until a diff
is submitted. Open **http://localhost:8002** after the workers print `READY`.
Paste a unified diff into the chat. In **Agent communication**, inspect
Parent → Subagent assignments, Subagent → Parent progress and findings, and any
follow-up exchanges. The reviewers are named for their angle and harness.

Or submit a diff file from another terminal (no LiteLLM environment needed there):

```sh
.venv/bin/python -m examples.code_review.run --diff examples/code_review/sample.diff
.venv/bin/python -m examples.code_review.run --diff /path/to/change.diff --instructions "Focus on backward compatibility"
```

The client prints a direct harness session link, message progress, and the final
review. It exits with code 2 if either specialist failed. Temporal histories are
at **http://localhost:8233/namespaces/code-review-demo/workflows**. Each UI request
uses a persistent coordinator; its `result` and `received_messages` queries expose
the final result and exact received envelopes. Signal `close` to finish the parent.
Ctrl-C in the launcher stops its processes; it leaves Temporal running.

## Review contract

`ReviewRequest` contains the diff and optional instructions. A future GitHub
fetcher can produce that same input without changing agent coordination.
The UI accepts pasted diffs; the CLI accepts files. This first example does not
fetch repositories or execute code/tests. It rejects missing text hunks and diffs
over 120,000 characters rather than silently truncating them.

Findings include severity, file, old/new side, line, explanation, and suggested fix.
Locations are checked against the supplied hunks; unsupported citations are
discarded and recorded as a limitation. Consolidation removes identical
location/title findings; differently worded findings remain visible.
Model/provider failures become explicit incomplete reports, never a clean review.
Reviewers are closed in a `finally` block after synthesis.

The sample intentionally contains authorization, injection, and empty-result
bugs. Findings are model-generated, so exact wording and coverage will vary.
The earlier `shared_workflow_tools` fixture demo remains separate; use this
example's registry and namespace to run actual code reviews.
