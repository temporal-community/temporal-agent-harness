# boltzmann — execution workspace & interactive review

A local development app around **the published Temporal Agent Harness package**. Open a Git repository, investigate it or request a small change, approve a plan, watch the agent work, approve checks, inspect the patch, and accept or continue.

**This is the handoff milestone. Further lifecycle development is intentionally paused for your testing.** The longer product direction remains in [PRODUCT.md](PRODUCT.md), [PLAN.md](PLAN.md), and [STATE_UI.md](STATE_UI.md).

New tasks use **Coding agent V2**: native OpenAI tools, bounded harness Code Mode, an implementer child, an independent reviewer child, and revision-bound verification. Existing tasks keep their V1 workflow and conversation. See [CODING_V2.md](CODING_V2.md) for the implemented scope, limits and test checklist; [AGENT_REVIEW.md](AGENT_REVIEW.md) preserves the original assessment.

**Milestone 3 is ready for testing:** a live execution workspace, per-file diffs, anchored human comments, persistent file review checkpoints, and targeted same-task follow-ups. Start with the [execution and review test guide](EXECUTION_REVIEW.md). GitHub delivery and CI feedback remain deferred until you test this milestone.

## Start

Requires Python 3.11+, `uv`, Node, `pnpm` (tested/pinned at 8.10.5), Git, and the Temporal CLI (`brew install temporal` on macOS). The initial target is macOS/Linux, one developer, one local app instance per data directory.

From the repository root:

```sh
./apps/sdlc/dev
```

Open the **launch URL printed in the terminal**. It unlocks the local application with an HTTP-only session cookie. The server binds to `127.0.0.1:8787`; its persistent Temporal server uses port `7333`. Keep the terminal running. Ctrl-C stops both owned processes; restarting restores tasks and approval gates.

**Temporal must be running for tasks to execute. You do not need to start it separately:** this launcher starts the persistent local Temporal server, the worker, and the web app together. Install the Temporal CLI first; no Docker, Temporal Cloud account, or separate harness server is needed for this local setup. Closing the browser is fine; stopping the launcher pauses execution until you launch it again. Ensure ports `8787` and `7333` are available (or use the port options below).

For a real task you also need a locally cloned Git repository with at least one commit, and an API key with access to the exact API model ID you enter. Save the key through **Settings** in your unlocked OS keyring, or export the configured environment variable **before launching** the app. The demo needs neither a provider account nor an API key. Checks may require project-specific runtimes/dependencies; review each proposed command and set those up for the project as needed.

The script installs the independent Python app, builds its Svelte UI, and starts the stack. It uses `temporal-agent-harness[ui,openai-agents,code-mode]==0.4.0` from the lockfile by default, including Monty. After updating, restart with this script once to install the new dependency. Your existing environment-variable/keyring profile setup stays the same. To test **local harness changes**:

```sh
./apps/sdlc/dev --local-harness
```

This installs the harness checkout as an ordinary editable dependency into the **app's** virtual environment. No source-path injection, example imports, harness fork, or changes to the harness package are required. To include changes to the harness's own frontend, also build that UI (`pnpm --dir ui build`) before launching. Running `./apps/sdlc/dev` again restores the locked published dependency.

After dependencies and UI are built, the fast start is:

```sh
cd apps/sdlc
.venv/bin/sdlc --data-dir .local
```

Options: `--port 8788`, `--temporal-port 7334`, `--data-dir /your/private/path`, or `--external-temporal 127.0.0.1:7233`. The external server is neither started nor stopped by the app. This is a local development setup, not a hosted service deployment.

## Your first test

1. Choose **Open the demo** and **Start task**. The labelled demo uses a fixed decision script, so it requires no API key. It exercises the real Temporal workflow, harness tool execution, state updates, filesystem, and test command.
2. In **Execution**, select an agent to inspect its blueprint, files, or verification evidence. **Follow live** follows the latest active role; **Remaining** shows unfinished steps. At **Approve this blueprint**, refresh the browser or restart the app. The same decision should still be waiting.
3. Approve the plan, then approve `python3 -m unittest -v`. Inspect pass/fail evidence in **Checks**. In **Review**, select a file, click a line to comment, and mark the file reviewed. **Since last review** compares each file with its last reviewed contents.
4. Open **Harness** to see the embedded packaged debugger, model/tool events, replay, and observable state. boltzmann owns task controls; debugger mutations are blocked at the API boundary.
5. Open **Files** to inspect or manually edit a file when the task is paused or ready for review. Edits invalidate earlier check evidence. Use **Request fixes** in Review to prepare selected comments in **Continue this task**, then review the message and send it. A fresh blueprint requires approval. Comments stay open until you resolve them.
6. **Accept** records your local review decision. After acceptance, **Merge into project** runs as a Temporal workflow update: the workflow authorizes the accepted revision, schedules a journaled filesystem activity, and records its receipt in workflow state. The activity preflights the accepted text patch and writes it into the configured source checkout as uncommitted changes. Existing non-overlapping source edits are preserved; overlaps and unsupported changes stop before the source is changed. **Patch** in Review remains available as a download fallback. Neither action pushes or creates a commit.

For your own work, add a repository and model profile in **Settings**. Start with **Ask the codebase** or a small, well-scoped change. The demo profile is restricted to the demo repository.

New provider profiles and tasks support **OpenAI** with an explicit API model ID and a model-call budget. Use a model that supports structured output through the Responses API. Enter a key to save it in the OS keyring, or reference an environment variable exported in the launch terminal. The app does not load the harness repository's `.env` files. Removing a profile removes its saved key; active tasks must first be stopped. To change a profile, add its replacement and select it for the next message or new task.

New OpenAI tasks use the **OpenAI Agents SDK through the harness's packaged integration**, streaming through `/v1/responses`. Server-side response storage and separate OpenAI tracing export are disabled; boltzmann supplies its own task context. After restarting boltzmann, your existing OpenAI profile can be used for new tasks and connection tests without replacing the model or key. Tasks created before this migration retain their original activity implementation so their saved histories can replay. Older Anthropic/compatible profiles remain visible for those existing tasks, but cannot start new tasks or connection tests in this milestone.

Select **Settings → Model profiles → Test connection** after saving a profile. This makes one small streamed model request using the same OpenAI Agents SDK, model settings, instructions, and structured action schema as a task, with automatic retries disabled and a 45-second time limit. Provider charges may apply. It reads the credential from the running app's environment or OS keyring; no repository content is sent and no task or workspace is created. The probe runs outside a workflow and does not require a model activity. The result shows credential lookup failures, the endpoint, HTTP status, rejected parameter, and the provider's message with credential redaction. These diagnostic results are temporary and are not saved in SQLite or Temporal history. Success verifies a single structured response, not that every future task will succeed.

For an environment reference, export the key in the **same terminal that starts boltzmann**, then launch from the repository root:

```sh
export OPENAI_API_KEY="your-api-key"
./apps/sdlc/dev
```

In the profile, leave **API key** empty and set **Or environment variable** to `OPENAI_API_KEY`. An already running app cannot see variables exported later; stop it with Ctrl-C and restart it in that terminal. No script edits or separate diagnostic command are needed. Restart after app updates too, then refresh the browser.

If a task fails, **Activity** shows an error card with a specific category, captured HTTP status/recognized provider code when available, and recovery steps. API keys and raw provider responses are excluded. **Provider settings** opens profile setup; **Continue** restores the failed message into the follow-up composer in the same task. Review the request, profile, and budget, then press **Send message**; continuing is not an automatic retry. Runs made before detailed diagnostics were added retain their original history and show a notice when the exact cause was not captured.

## Continue a conversation

In **Activity → Continue this task**, ask a follow-up question, refine the feature, or describe an issue to fix, then select **Send message** (`⌘/Ctrl Enter`). The task ID, Temporal workflow, workspace, prior activity, changed files, and check evidence stay together. **Accept** records review without closing the conversation. You can also continue failed or stopped tasks, including tasks created before this feature; their previous failure remains in Activity and Harness history.

Choose **Ask (read only)** or **Make changes**, a model profile, and a budget of **3–200 model calls for this message**. The default comes from the selected profile. Each message gets its own allowance; the sidebar shows both the current message's usage and lifetime task totals. A new change message always needs a fresh plan approval, and each command still needs approval. Later writes invalidate previous checks. Switching model profiles carries the retained conversation and repository context to that model.

You can draft while the agent works. Send after it finishes, or select Stop and wait for it to stop. At an approval or question, use the existing response controls first; at a pause, resume or stop. There is no queue or mid-action redirection in this milestone. Save or discard unsaved file edits before sending. Drafts are stored per task in this browser; accepted messages and execution settings are durable. If delivery is interrupted, boltzmann retries the same message ID after reconnect/restart to avoid duplicate execution, and shows an undelivered message if the agent rejects it.

The agent receives the original request and retained conversation/tool results, and is instructed to reread files before editing. Older context is bounded to 120,000 characters between messages by dropping whole older records; the sidebar reports when this happens. Full activity and harness history remain available. Long tasks still share one Temporal history; automatic summarization and continue-as-new are future work. For a separate experiment or a fresh context, **Fork task** creates another task from the current workspace with a short handoff summary.

## Delete a task

Open a task and select **Delete** next to its progress controls. Tasks that are working, paused, or waiting for approval must first be stopped; wait for **Stopped** before deleting. Tasks ready for review, accepted, failed, or stopped can be deleted, as can interrupted workspace preparations.

The confirmation dialog keeps workspace files by default and shows their location. Select **Also permanently delete this task’s isolated workspace** to remove that clone and its changes as well. The source repository and other task workspaces stay intact. Deletion removes the task from boltzmann and closes its Temporal execution; it cannot be continued from the app afterward. Temporal history, operation journals, and offloaded payloads remain under their existing retention policies. This is task cleanup, not erasure of every audit record.

boltzmann checks the current execution before deleting. If Temporal cannot be reached, the task and files are kept for retry. If workspace removal fails after the execution closes, the task stays listed so you can retry cleanup or choose to keep the remaining files.

## What is implemented

- A real V2 controller and role child workflows using `AgentWorkflowRunner` and `runner.state("sdlc", CodingState())`. All nested observable models use `HarnessState`. The workflow mutates state synchronously; the harness emits its native snapshots and patches. V1 remains registered for existing histories.
- Native OpenAI Agents SDK model activities via `OpenAIAgentsPlugin`, with the harness observer providing model streams, spans, and usage. Credential values are resolved inside model activities and do not enter workflow arguments. Connection testing uses the same SDK configuration.
- A custom progress UI showing controller stages, active roles/operations, blueprint requirements, check freshness, independent findings, changed files and aggregate usage. The controller derives verification from actual command receipts and a review of the same revision; a model summary alone cannot mark work verified.
- Durable plan and command gates, question/answer, bounded model calls, pause at action boundaries, stop, local acceptance, and ongoing conversations with per-message budgets, modes, and provider profiles.
- SQLite project/profile/task catalogue and archived full state snapshots. The browser refreshes authoritative snapshots every two seconds; reconnect replaces the whole document. **The first milestone deliberately uses full snapshots rather than the future SSE/patch projector.** The native harness debugger still consumes harness events directly.
- Separate Git clones at committed HEAD, a private task branch, path/secret/generated-file exclusions, no symlink traversal, text-file limits, stale-hash rejection, atomic writes, and an operation journal. The source checkout's uncommitted/untracked files are not copied; the UI reports when the source was dirty.
- File inspection/manual editing, actual command exit codes/output, tracked and new text-file diffs, patch export, workflow-owned guarded merge into the configured source checkout, task history/search, draft persistence, keyboard shortcuts (`⌘/Ctrl K`, `⌘/Ctrl ,`), and opt-in desktop notifications. Merge activities use operation journals and per-project OS locks; completed receipts remain in observable Temporal state across app restarts.
- GitHub remote links for existing checkouts; immutable per-message provider configuration; credential references rather than secret values in workflow history and SQLite; same-origin local authentication and request checks.

## Deliberate limits of this milestone

**Commands run on your host after individual approval. They are not container-sandboxed.** They can access host files and the network. Their environment excludes inherited provider keys and uses a private temporary home, but that is not a security boundary. Review the command and repository before approving. Commands have a 90-second/output limit and uncertain interrupted commands are not automatically repeated. Read/search/write tools enforce workspace boundaries; that restriction does not sandbox an approved host command.

V2 uses the SDK's native tool loop, with structured proposals/results at role boundaries. The controller owns approvals, permissions and deterministic check execution. `src/sdlc_builder/integrations/` exposes a role-execution contract alongside the V1 decision contract, allowing future Pydantic AI integration. The old Pydantic AI dependency/activity remains for legacy replay; new OpenAI tasks use the OpenAI Agents SDK.

boltzmann includes a small serialization compatibility layer for harness 0.4.0: OpenAI fields use their wire aliases when written into Temporal payloads. This fixes structured responses whose `schema` metadata was otherwise stored as `schema_` and could be decoded as the wrong stream-event type. The harness source has the same fix; the compatibility layer lets the default published-package setup work before a new harness release. Existing histories keep their original decoding behavior. After updating, restart boltzmann and use **Continue → Send message** for an affected task; its old failure record is preserved.

The published package also needs a stream-observer adapter for function-call completion events that omit `name`. The harness source now takes the tool name from the opening item, and boltzmann adapts the observer's event copy for the published package. Without this fix, the provider can respond successfully but the task fails with `ResponseFunctionCallArgumentsDoneEvent ... has no attribute 'name'`. A passing connection test checks credentials, native tool calling, and structured output; it does not exercise the Temporal activity's harness observer. No key or profile change is needed for this error: restart the app and continue the affected task.

It is best suited to questions and focused text-file changes. It has no semantic index, language server, interactive terminal, delete/rename tool, automatic dependency setup, branch publishing, PR/CI integration, preview server, deployment adapter, automatic rollback, or shared-service authentication yet. Those are planned after your feedback. Deployment configuration is documented in the plan, not presented as a working control.

V2 stage status records controller progress; requirements still need your review. Check and review receipts identify the source revision. App edits invalidate evidence immediately; revision reads before/after checks and review, and again on acceptance, detect external edits. External edits are not continuously watched. Incomplete verification requires an explicit acceptance note. V1 retains its original progress semantics. Browser drafts are local to that browser; task state and evidence live in Temporal/SQLite.

Secrets entered through Settings never appear in application readback. Repository content and check output become task history and may be sent to the selected model; file exclusions are defensive defaults, not a guarantee that arbitrary source code contains no secrets. Keep the data directory private. The embedded Temporal dev server is suitable for local development only.

## Development and verification

```sh
cd apps/sdlc
.venv/bin/pytest -q                         # focused tests; live test skips by default
SDLC_INTEGRATION=1 .venv/bin/pytest tests/test_live.py tests/test_coding_live.py -q
.venv/bin/ruff check --config pyproject.toml src tests
pnpm --dir ui check
pnpm --dir ui build
```

The integration test launches its own isolated local stack and exercises the demo and native OpenAI SDK lifecycle, restarts at approval, stale approval rejection, actual test execution, patch preservation, observable-state event replay, manual-edit evidence invalidation, and task deletion across restart. It also covers same-task follow-ups, Ask/Change switching, repeated writes, fresh approval gates, cumulative usage, per-message budgets, and recovery from failures/cancellation. OpenAI tests run the actual SDK against a local Responses stream fixture; a separate replay test checks a saved pre-migration history. **No paid provider call is made by this suite.** Configure your own profile to test live model quality and account access.

Data under `.local/`: `app.sqlite3`, `temporal.sqlite3`, `temporal.log`, private workspace clones, operation journals, payload offload, command homes, and the local session token. These are ignored by Git. Back up the whole directory together. Interrupted workspace creation preserves its partial directory and reports a preparation error; delete it when ready and start a new task. There is no automatic workspace or history cleanup yet.

Useful feedback at this checkpoint: whether the approvals feel clear, whether progress makes the agent's next action obvious, whether Ask answers your real repository questions, whether the edit/check/review loop feels convenient, and which missing daily-development feature should come next.
