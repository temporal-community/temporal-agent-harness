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

## Temporal Cloud Actions

On Temporal Cloud, Queries and Updates are billable Actions. Each attach is two Queries and an
Update per batch of events, and each idle status check is one Query. While the agent is idle,
a session checks for work other clients gave it every `idlePollMs` (30s by default, 0 for
never), and not while the page is hidden. It checks once as soon as the page is shown again.

In a browser with Web Locks and BroadcastChannel, tabs following the same session share one
stream (`shareAcrossTabs`). It is on by default unless the session has `callbackTools`; pass
`false` to turn it off:
- **Leader:** the tab holding the session's lock attaches, polls, and runs `callbackTools`. It
  relays what it receives to the others, so N tabs cost the Actions of one.
- **Followers:** the other tabs still send messages and answer approvals themselves.
- **Takeover:** when the leader closes, another tab takes over from the last event it has.

With `callbackTools`, the followers would never answer a call, so each tab keeps its own stream
by default. Pass `shareAcrossTabs: true` only when one tab answering every call is what you
want, as with a single-editor app.

To look at a session from the browser console during development, expose it:

```ts
if (import.meta.env.DEV) exposeSession(sessionId, state);
// In the console: __AGENT_HARNESS__["<session id>"]()
```

That returns `inspectSession(state)`: a plain object with the connection, the last resume offset,
counts of frames, pending sends and waiting tool calls, and each agent's status and state ids.
Both functions take an `AgentSessionCore`'s `state` or a binding's session object. Given the
`AgentSessionCore` itself, the result also has `activity`:
- the frame rate over the last 10 seconds;
- counts of attaches, status checks, sends, approvals, callback results and closes;
- an estimate of the Temporal Cloud Actions those cost;
- whether this tab is the `leader`, a `follower` or `solo`.

As a [TanStack Devtools](https://tanstack.com/devtools) panel, loaded only in development:

```ts
if (import.meta.env.DEV) {
  void import("@tanstack/devtools").then(({ TanStackDevtoolsCore }) => {
    let stop = () => {};
    new TanStackDevtoolsCore({
      plugins: [{
        id: "agent-harness",
        name: "Agent session",
        render: (el) => {
          stop();
          const pre = el.appendChild(document.createElement("pre"));
          const paint = () => void (pre.textContent = JSON.stringify(inspectSession(session), null, 2));
          paint();
          const unsubscribe = session.onFlush(paint);
          const timer = setInterval(paint, 1_000); // connection and rates change between flushes
          stop = () => { unsubscribe(); clearInterval(timer); el.replaceChildren(); };
        },
        destroy: () => stop()
      }]
    }).mount(document.body.appendChild(document.createElement("div")));
  });
}
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
