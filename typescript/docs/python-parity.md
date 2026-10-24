# Parity with the Python SDK

The TypeScript SDK (`@temporalio/agent-harness`, `@temporalio/agent-harness-worker`) follows the
Python SDK (`python/temporal_agent_harness/`): the same protocol (`contract/protocol.schema.json`),
the same behavior where both can express it, and the same layout. A Python module's TypeScript
counterpart is at the same path with the same words in kebab-case
(`harness/agent_workflow.py` is `src/harness/agent-workflow.ts`), in `core` for code that runs in
the workflow and in `worker` for code that runs in activities or the worker process.

## Where each Python module lives

Paths are under `python/temporal_agent_harness/`, `typescript/packages/core/src/` (core) and
`typescript/packages/worker/src/` (worker).

| Python | TypeScript |
| --- | --- |
| `harness/agent.py` | core `harness/agent.ts` |
| `harness/agent_workflow.py` | core `harness/agent-workflow.ts`, `harness/agent-workflow/{errors,types}.ts`; worker `harness/agent-workflow.ts` (activity tools) |
| `harness/agent_client.py` | worker `harness/agent-client.ts` |
| `harness/agent_protocol/` | core `harness/agent-protocol/`; the protocol types come from `@temporalio/agent-harness-client` |
| `harness/agent_schema.py` | core `harness/agent-schema.ts` (the document); worker `harness/agent-schema.ts` (loading an agent and writing it: the `agent-harness-schema <module>#<export>` bin, the TypeScript half of `temporal-agent-harness schema`) |
| `harness/stream_context.py` | core `harness/stream-context.ts` |
| `harness/subagent_toolset.py` | core `harness/subagent-toolset.ts` |
| `harness/subagent_activities.py` | worker `harness/subagent-activities.ts` |
| `harness/state/` | core `harness/state/index.ts` |
| `harness/stream_merge/` | worker `harness/stream-merge/index.ts` |
| `harness/code_mode/{driver,tool,stubs,vfs,okf,okf_tool,mount_decl,activity_fs}.py` | core `harness/code-mode/` at the same names, with `base64.ts` (the workflow sandbox has no `Buffer`); the activity side of `activity_fs` and `okf` in worker `harness/code-mode/` |
| `harness/code_mode/monty_stepper.py` | worker `harness/code-mode/monty-stepper.ts`, with `secret.ts` (the key that seals snapshots in history) |
| `harness/code_mode/_texts.py` | core `harness/code-mode/texts.ts`; both generated from `contract/code_mode/` |
| — | core `harness/py-repr.ts`: Python's `repr()`, which the Python harness shows the model |
| `plugin.py` | worker `plugin.ts`; `runAgentWorker` in worker `index.ts` |
| `utils/large_payload.py` | worker `utils/large-payload.ts`, `utils/s3.ts` |
| `ai_sdks/jev/evaluator.py` | core and worker `ai-sdks/jev/evaluator.ts` |
| `ai_sdks/integration_helpers/` | core `ai-sdks/integration-helpers/raw-events.ts` (model streams translated in the workflow) |
| `web/client.py` | worker `web/client.ts` |
| the rest of `web/`, `ui/` | None: the server, session manager, registry, the rest of the CLI and the console have one implementation, which serves agents written with either SDK |
| `ai_sdks/openai_agents*`, `ai_sdks/google_genai_plugin/` | Stage 5's integrations PR (`openai-agents`, `gemini`, plus `ai-sdk`) |
| `ai_sdks/pydantic_ai_harness.py` | None: Pydantic AI is Python-only |
| `ai_sdks/typesafe_harness.py` | Not ported yet |
| `nexus_agent_adapter/` | None: Nexus is not part of the TypeScript SDK |

## Differences

Every difference is listed here. A change to one SDK's behavior updates the other in the same
change, or adds a row here saying which SDK has it and why.

### Forced by the languages

| Area | Python | TypeScript | Why |
| --- | --- | --- | --- |
| Where Code Mode runs Monty | In the workflow (the sandbox passes `pydantic_monty` through); the paused interpreter stays in workflow memory | In activities; each paused script is snapshotted, sealed with an HMAC, and passed through history | A TypeScript workflow isolate can't load native code. Parity is a follow-up to investigate (Monty's WebAssembly core) |
| Code Mode's time limit | A script stops after 1 s without awaiting a host function | The same, but cut to 80% of the step activity's start-to-close timeout when that is shorter; the error still says 1 s | A script runs inside an activity, and a script that outlived the activity would time out and retry instead of failing on its limit |
| `os.urandom` in Code Mode | `random.Random` seeded from `workflow.random()`; a bad size raises `randbytes`'s errors | SHA-256 of the workflow's seed and the call id; a bad size raises `TypeError: os.urandom() takes an integer size` or `ValueError: negative argument not allowed` | Both are deterministic on replay, but the TypeScript activity can't share the workflow's generator, so the bytes and the error wording differ |
| Deleting a missing key in a state draft | `del d.values["k"]` raises `KeyError` | `delete d.values.k` does nothing | State drafts are immer recipes, and JavaScript's `delete` of an absent key is not an error |
| Values JSON can't carry exactly | Accepted (unbounded ints) | Rejected with `NonPortableJson`: ints past 2^53, lone surrogates, nesting past 200 | A JavaScript number can't hold a larger integer exactly |
| Decorators | `@agent.defn`, `@agent.accepts`, `@agent.tool_defn` | `defineAgent`, `handler`, `tool`, `activityTool` | TypeScript has no decorators on functions; declarations are data plus zod schemas |
| Ambient context | `contextvars` | `AsyncLocalStorage` | Language |
| Schemas | pydantic models | Any Standard Schema that renders JSON Schema (zod 4); rendered in zod's own input and output modes, so documents are equivalent to Python's but not byte-identical | Language |
| Starting a worker | A `Worker` with `AgentHarnessPlugin` | `runAgentWorker`, which also bundles the agents module as the workflows module | The TypeScript worker bundles workflow code |
| A subagent send tool's input key | The child handler's own parameter name, read from its signature | The handler's `inputName` (default `message`) | A JavaScript function's parameter names can't be read reliably |
| A Code Mode script's Monty crash (not a timeout) | A fixed crash message | `MontyCrashedError: <message>` from the Node binding | The two Monty bindings report a crash differently |
| File errors from an in-workflow VFS mount | Python's `OSError` wording | `[Errno n] … '<virtual path>'`; activity-backed mounts give Python's wording | The in-workflow filesystem formats its own errors |
| Cancelling a stream poll from a worker | The RPC is cancelled | The poll is abandoned and ends when the server answers; the worker's native connection ignores abort signals | `@temporalio/worker`'s `NativeConnection` doesn't support aborting a call |

### In TypeScript first, Python to follow

| Area | Python | TypeScript |
| --- | --- | --- |
| Activity tools with a heartbeat timeout | The activity never heartbeats, so a long tool call fails its heartbeat timeout | The activity heartbeats at half the timeout while the tool runs |
| Stream polls parked when a run completes | Left unanswered; the run completes with the poll update still in flight | Answered with the last events, `turn_end` included (`detachPollers`), before the run completes |
| A failed subagent turn | `subagent_message_sent` is never closed: the runner catches `ApplicationError`, but a failed activity raises `ActivityError` wrapping it | `subagent_reply_received` with `outcome: "error"` closes the bracket |
| A tool approval sent after `close` | Accepted, so the call can still run | Rejected with `AgentClosed` |

### Not yet the same

- `tool_end.tool_output` is the result as Python's `str()` renders it in both SDKs, which is a
  Python repr for a structured result; the protocol is to carry structured results instead (stage
  5b of the polyglot monorepo plan).
