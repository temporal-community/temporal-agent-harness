# @temporalio/agent-harness-client

A framework-independent client for temporal-agent-harness agents. `AgentSessionCore` follows one
session's merged event stream — the root agent and every subagent it drives — and projects it
into messages and observable state for each agent, through a `SessionState` a framework binding
supplies (`@temporalio/agent-harness-svelte`, `@temporalio/agent-harness-react`, or
`PlainSessionState` for none).

```ts
import { AgentSessionCore, HttpTransport, PlainSessionState } from "@temporalio/agent-harness-client";

const state = new PlainSessionState<TicTacToeAgent>();
const session = new AgentSessionCore(
  { sessionId, transport: new HttpTransport({ baseUrl: "/api/" }) },
  state
);
state.subscribe(() => render(state.messages, state.states.board));
session.start();
await session.sendMessage("play", { cell: 5 });
```

`src/protocol.ts` is the event stream's wire types, generated from `events.py`:
`bazel run //typescript/packages/client/src:protocol_codegen_update` regenerates it, and `bazel test` fails
when it is out of date.

## Development

```sh
bazel test //typescript/packages/client/...   # one target per test file, e.g. //typescript/packages/client/test:session_test
```

Without Bazel, from this directory:

```sh
pnpm install
pnpm test   # compiles src/ and test/ (including compile-time type checks), then runs node --test
```
