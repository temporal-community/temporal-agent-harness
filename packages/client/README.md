# @temporal-agent-harness/client

A framework-independent client for temporal-agent-harness agents. `AgentSessionCore` follows one
session's merged event stream — the root agent and every subagent it drives — and projects it
into messages and observable state for each agent, through a `SessionState` a framework binding
supplies (`@temporal-agent-harness/svelte`, `@temporal-agent-harness/react`, or
`PlainSessionState` for none).

```ts
import { AgentSessionCore, HttpTransport, PlainSessionState } from "@temporal-agent-harness/client";

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
`npm run generate:protocol` regenerates it (it needs `packages/codegen` and `uv`).

## Development

```sh
npm ci
npm test    # compiles src/ and test/ (including compile-time type checks), then runs node --test
```
