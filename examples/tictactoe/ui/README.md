# Tic-Tac-Toe board

A Svelte page for the tic-tac-toe agent, written against `@temporal-agent-harness/svelte` and the
agent's generated types (`../client_sdk/TicTacToeAgent.ts`, from `just codegen-client-sdk`). Run
it with `just play` from `examples/tictactoe`, then open http://127.0.0.1:5173/.

The whole page is `App.svelte`, and its whole integration is the top of that file:

```svelte
<script lang="ts">
  import { AgentSession, HttpTransport } from "@temporal-agent-harness/svelte";
  import type { TicTacToeAgent } from "../client_sdk/TicTacToeAgent";

  const agent = new AgentSession<TicTacToeAgent>({
    get sessionId() { return sessionId; },
    transport: new HttpTransport({ baseUrl: "http://localhost:8000/api/" })
  });

  const board = $derived(agent.states.board); // the agent's `Board` model, kept current
  const play = (cell: number) => agent.sendMessage("play", { cell });
  const newGame = (agent_goes_first: boolean) => agent.sendMessage("new_game", { agent_goes_first });
</script>
```

- `agent.states.board` is the agent's observable state, kept current from the event stream.
- `agent.sendMessage(handler, payload)` is checked against the agent's handlers and their input
  models.
- `agent.messages`, `agent.frames` and `agent.pendingApprovals` drive the last judgment, the
  ledger and the approve / deny buttons.
- Your move is drawn the moment you click: a `play` still in `agent.pending` or running in
  `agent.messages` puts your mark on its cell until the board's own patch lands.

## No build step

`index.html` maps `svelte` to esm.sh and the harness packages to `harness-client/` and
`harness-svelte/`, which `just play` links to those packages' builds in this repo, and
`svelte-sw.js`, a service worker, compiles `App.svelte` (and the binding's `.svelte.js` rune
modules) as the browser fetches them. Svelte 5 strips the component's TypeScript itself, so the
generated types cost nothing at runtime. Node is needed only to build the two harness packages,
which `just play` does.

The types still count: `just check-ui` (and CI) type-check `App.svelte` against the generated
`TicTacToeAgent`, so a Python model change that breaks the page fails there.

The session is in the URL (`?session=…`): reload to replay it, or follow "new session" for a
fresh one.
