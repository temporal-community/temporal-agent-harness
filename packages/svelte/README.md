# @temporal-agent-harness/svelte

A Svelte 5 binding for `@temporal-agent-harness/client`: `AgentSession`, a reactive handle on one
harness agent session.

```svelte
<script lang="ts">
  import { AgentSession } from "@temporal-agent-harness/svelte";
  import type { TicTacToeAgent } from "./client_sdk/TicTacToeAgent"; // from harness-codegen

  let { sessionId } = $props();
  const agent = new AgentSession<TicTacToeAgent>({ get sessionId() { return sessionId; } });
  const board = $derived(agent.states.board);
</script>

{#if board}
  {#each board.cells as cell, i (i)}
    <button onclick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button>
  {/each}
{/if}
```

- **Reading opens the connection.** The first effect or markup to read any field opens the
  session's stream; it closes once nothing reads it, so unmounting the last view releases it.
- **Getter options follow changes.** A `get sessionId()` switches the session when its value does.
- **One connection per session.** Call `createHarnessContext()` in a parent component and every
  `AgentSession` below it with the same session id shares one connection and one state.
- **The whole tree.** `messages`, `states` and `agentStatus` are the root agent's; `agents` and
  `agent(id)` reach every subagent, and a `subagent` part's `messageId` names the child message
  it ran. `pendingApprovals` and `pendingCallbacks` collect waiting tool calls from all of them.

## Development

This package links `../client` and resolves it through that package's build:

```sh
(cd ../client && npm ci && npm run build)
npm ci
npm run check && npm test && npm run build
```
