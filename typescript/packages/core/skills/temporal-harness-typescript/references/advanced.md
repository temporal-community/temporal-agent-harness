# Advanced TypeScript features

Read the installed package's public types and the agent's generated schema for exact signatures.
Prefer a small change to the working starter over copying internal runtime implementation.

- **Observable state:** declare state on the agent, initialize it through its schema, and mutate
  through the provided state reference. Mutations publish patches; changing an unrelated local
  object does not publish observable state. Keep mutation callbacks synchronous.
- **Approvals:** distinguish permission to run a tool from supplying a callback result. Preserve
  the requesting agent and tool IDs, especially for subagents. See `api.md` for HTTP calls.
- **Callbacks:** the tool implementation runs on the client; model execution waits for a result.
  Use the application client's callback handlers or pending callback interface.
- **Subagents:** expose typed handlers and compose through the harness subagent APIs. Follow
  events using their agent identity rather than flattening the tree into the parent's transcript.
- **Code Mode:** requires the optional Monty sandbox dependency on the worker. Expose an explicit
  toolset; scripts run against those host functions. Filesystem mounts and durable activity
  filesystems have separate declarations and worker registrations.
- **Mid-turn handlers:** choose enqueue/accept/reject intentionally. Use `/api/messages` plus
  `/api/attach` for clients that support messages joining an existing turn.

For complete worked examples matching a release, use the repository's release-tagged Monty and
OpenAI examples. A checkout is optional for normal application development. Do not assume Python
and TypeScript model integrations or advanced feature signatures are interchangeable.

## Add observable state to the starter

Import `state` from `@temporalio/agent-harness`. Add a declaration to `defineAgent` and read its
reference from the `create` context:

```ts
// Alongside handlers in defineAgent({...}):
states: { counter: state(z.object({ messages: z.number().int().default(0) })) },

// In create({ runner, state }), inside a handler:
state.counter.mutate(draft => { draft.messages += 1; });
```

Read `state.counter.current` for the current value. Mutation callbacks must be synchronous.
Regenerate the agent schema; client codegen then exposes the `counter` state.

## Choose a tool boundary

| Work | Public declaration | Registration |
| --- | --- | --- |
| Deterministic, fast computation | `tool({ ..., execute })` | Adapt through the model integration |
| External I/O, database or provider calls | `activityTool({ ..., activityOptions })` | `new AgentHarnessPlugin({ tools: [toolActivity(declaration, implementation)] })` on the worker |
| Browser/user environment | `tool({ ..., outputSchema })` without `execute` | Supply the result through the application client |

Declarations come from `@temporalio/agent-harness`; `toolActivity` comes from
`@temporalio/agent-harness-worker`. Keep implementations with Node/network imports out of the
workflow bundle. Set timeouts and make side effects idempotent across retries. `inherentlySafe`
is an explicit approval-policy decision. Use `injected<T>()` bindings for workflow-supplied
values that should stay out of model input schemas.

`subagentToolset` and `codeModeTool` are exported from the core package. Code Mode also needs
`@pydantic/monty` on the worker. Inspect their installed declarations for lifecycle, mounting,
and toolset options. Keep each child's registry, worker queue, and generated interface aligned.
