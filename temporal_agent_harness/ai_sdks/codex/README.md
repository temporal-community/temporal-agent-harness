# Codex CLI integration

`CodexPlugin` runs Codex as the **inner agent loop** of a harness turn. Codex chooses and executes its tools; Temporal owns the surrounding activity, result,cancellation, and event history. The plugin translates the CLI's JSONL events into the same native model/tool/reply events used by the other SDK integrations and the packaged UI.

No additional Python SDK is required beyond the harness's core dependencies. Install
Codex CLI separately and authenticate with `codex login` on the worker host. The
plugin uses saved CLI authentication; it does not require a separate OpenAI API key.
Verified with Codex CLI 0.153.4; older versions must support the `--ephemeral`,
`--ignore-user-config`, and `--ignore-rules` execution flags.
See [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

## Wire an agent

```python
# Worker
from temporalio.client import Client
from temporalio.worker import Worker
from temporal_agent_harness.ai_sdks.codex import CodexConfig, CodexPlugin

plugin = CodexPlugin(CodexConfig(workspace="/path/to/repository"))
client = await Client.connect("localhost:7233", plugins=[plugin])
worker = Worker(client, task_queue="my-codex-agent", workflows=[MyAgent])
await worker.run()
```

The client plugin supplies a Pydantic-compatible data converter and automatically
registers the `codex_execute` activity on workers created from the client. The CLI
binary, workspace, model, sandbox, and timeout are worker configuration, not chat input.

```python
# Inside an @agent.accepts handler, with an active AgentWorkflowRunner
from temporal_agent_harness.ai_sdks.codex.workflow import run_codex

result = await run_codex("Inspect the repository and explain its test setup.", runner=self._runner)
return TextReply(text=result.text)
```

The [Codex Hello example](../../../examples/codex_hello) provides the complete workflow,
worker, client, registry, and UI startup commands. It retains bounded conversation
context in workflow state and passes it to each fresh Codex invocation. The plugin
does not implicitly resume CLI threads; `result.thread_id` is diagnostic metadata.

## Events

| Codex event                            | Harness UI/event behavior                                                                                 |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| CLI invocation begins/ends             | `model_interaction_started` / `model_interaction_ended`, including reported token usage                   |
| Command execution                      | Native `codex.command` requested/start/progress/end or error, with command, bounded output, and exit code |
| File change                            | `codex.file_change` lifecycle with changed file paths; patch bodies omitted                               |
| Web search / MCP call                  | Native tool lifecycle with query or server/tool name; MCP arguments/results omitted                       |
| Plan update                            | `codex.plan` progress with checklist status                                                               |
| Completed agent message                | `reply_delta`; final CLI reply becomes the handler result                                                 |
| Process failure, timeout, cancellation | Active tool cards settle as errors and the model span closes                                              |

Tool IDs are namespaced per invocation, so repeated CLI item IDs do not collide.
Codex reports completed text snapshots and action updates; it does not promise a
token-by-token reply stream. Reasoning is omitted. Unknown or malformed JSONL records
are ignored; oversized lines are discarded. A missing successful completion event or
missing/empty/oversized final reply fails the activity instead of claiming success.

Each display payload is bounded to 8 KiB, with at most 256 tool identities and 256
message identities retained per invocation. Common credential patterns are redacted;
this is best effort, so prompts, commands, and emitted text should contain no secrets.
The worker continuously retains a 64 KiB raw log tail and the final reply under
`.codex-runs/` (configurable), separate from the events forwarded into Temporal.

## Execution settings and limits

`CodexConfig` defaults to `sandbox="read-only"`, a 600-second process deadline, the
installed `codex` binary, and the CLI's default model. Set `model` explicitly to choose
one. `sandbox="workspace-write"` enables coding in the configured workspace. The
plugin always disables shell network access, uses `approval_policy="never"`, ignores
user configuration/rules, and never requests an unrestricted sandbox.

**Codex's native tools are governed by that worker configuration.** Harness per-tool
approval policies do not intercept them: native tool cards report Codex execution.
Use a dedicated, appropriately scoped workspace when enabling writes. This integration
does not implement branch management, publishing, deployment, or production approval.

Each invocation is one Temporal activity, with a five-second heartbeat, thirty-second heartbeat timeout, and **one attempt**. The process deadline is configurable up to one hour; the enclosing activity has a two-hour ceiling. A completed activity replays from its saved result without rerunning Codex. Individual inner tool actions are not separate Temporal checkpoints, so interrupted work may have changed files: inspect the workspace and deliberately submit another turn rather than automatically retrying side effects.

The current process-group cleanup requires a POSIX worker. A plugin instance serializes calls to its workspace; configure one owning worker per writable workspace/task queue. Workspace storage and local CLI authentication must remain available on that worker.
