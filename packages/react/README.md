# @temporal-agent-harness/react

A React binding for `@temporal-agent-harness/client`: `useAgentSession`, a hook that follows one
harness agent session. React 18.2 or later.

```tsx
import { useAgentSession } from "@temporal-agent-harness/react";
import type { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen

function Board({ sessionId }: { sessionId: string }) {
  const agent = useAgentSession<TicTacToeAgent>({ sessionId });
  const board = agent.states.board;
  if (!board) return null;
  return board.cells.map((cell, i) => (
    <button key={i} onClick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button>
  ));
}
```

- **Mounting opens the connection.** The first mounted component using a session opens its
  stream; it closes once the last one unmounts. StrictMode's extra unmount and remount in
  development does not reconnect.
- **Renders follow the stream.** The hook reads the session through `useSyncExternalStore`, and
  the core writes once per flush, so a burst of frames is one render.
- **A changed `sessionId` moves the connection.** Pass it as a prop or state like any other value.
- **One connection per session.** Put a `<HarnessProvider>` above your components and every
  `useAgentSession` below it with the same session id shares one connection and one state.
  Without one, each component gets its own.
- **The first component to open a session decides its options** (`callbackTools`, transport and
  so on). Later components, and later renders, share what it set up, so passing a new
  `callbackTools` object each render is fine but does not replace the handlers.
- **The whole tree.** `messages`, `states` and `agentStatus` are the root agent's; `agents` and
  `agent(id)` reach every subagent, and a `subagent` part's `messageId` names the child message
  it ran. `pendingApprovals` and `pendingCallbacks` collect waiting tool calls from all of them.
- **Server rendering** shows the empty session and opens no connection; the client connects once
  it hydrates.

## Development

This package links `../client` and resolves it through that package's build:

```sh
(cd ../client && npm ci && npm run build)
npm ci
npm run check && npm test && npm run build
```
