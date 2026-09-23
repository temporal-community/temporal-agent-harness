# Tic-Tac-Toe board

A Svelte app for the tic-tac-toe agent, built on `@temporal-agent-harness/svelte` and the
agent's generated types (`../client_sdk/TicTacToeAgent.ts`, from `just codegen-client-sdk`).
Run it with `just play` from `examples/tictactoe`.

The whole integration is one typed `AgentSession`:

```svelte
<script lang="ts">
  import { AgentSession, HttpTransport } from "@temporal-agent-harness/svelte";
  import type { TicTacToeAgent } from "../../client_sdk/TicTacToeAgent";

  const agent = new AgentSession<TicTacToeAgent>({
    get sessionId() { return sessionId; },
    transport: new HttpTransport({ baseUrl: "http://localhost:8000/api/" })
  });
  const board = $derived(agent.states.board);   // typed as the agent's Board model
</script>

<button onclick={() => agent.sendMessage("play", { cell: 5 })}>Play 5</button>
```

- `agent.states.board` is the agent's observable state, kept current from the event stream.
- `agent.sendMessage(handler, payload)` is checked against the agent's handlers and their input
  models.
- `agent.messages`, `agent.frames` and `agent.pendingApprovals` drive the ledger, the last
  judgment and the approve / deny buttons.
- `createHarnessContext()` in `SessionView.svelte` lets `Game.svelte` and `Ledger.svelte` each
  construct an `AgentSession` for the same session while sharing one connection.
