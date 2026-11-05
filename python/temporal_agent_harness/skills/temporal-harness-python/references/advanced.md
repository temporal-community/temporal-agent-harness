# Advanced Python features

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

Import `HarnessState` from `temporal_agent_harness.harness.state`. Define a model outside the
agent and declare its state as a class attribute:

```python
class Counter(HarnessState):
    messages: int = 0

# Inside OpenAIHelloAgentWorkflow:
counter = agent.state(Counter)

# Inside a handler, before returning:
with self.counter.mutate() as draft:
    draft.messages += 1
```

Read `self.counter.current` for the current value. Do not hold a mutation context across an
`await`. Regenerate the agent schema; client codegen then exposes the `counter` state.

## Choose a tool boundary

| Work | Public declaration | Registration |
| --- | --- | --- |
| Deterministic, fast computation | `@agent.tool_defn` | Adapt it through the model integration |
| External I/O, database or provider calls | `@agent.activity_tool_defn` | Also pass it in `AgentHarnessPlugin(tools=[...])` |
| Browser/user environment | `@agent.callback_tool_defn` | Supply the result through the application client |

Keep the decorated activity body importable on the worker. Set timeouts and make side effects
idempotent across retries. `inherently_safe=True` is an explicit approval-policy decision, not
a substitute for idempotency. `agent.Injected[T]` parameters come from workflow bindings and
stay out of the model's input schema.

`agent.subagent_toolset` exposes child-agent handlers as model tools. `agent.code_mode_tool`
combines tools into a sandbox interface; install the `code-mode` extra. Inspect these exported
callables' signatures for the installed version before supplying lifecycle or filesystem options.
