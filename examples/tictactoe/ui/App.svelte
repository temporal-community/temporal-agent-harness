<!-- The tic-tac-toe board. Everything the page knows about the agent comes through `agent`, a
     typed AgentSession: the board is its `board` state, a move is a `play` message, and the
     ledger is its raw frames. The rest of this file only draws what those give it. -->
<script lang="ts">
  import { AgentSession, HttpTransport } from "@temporal-agent-harness/svelte";
  import { scale } from "svelte/transition";

  import type { TicTacToeAgent } from "../client_sdk/TicTacToeAgent";

  const API = "http://localhost:8000/api/";

  let sessionId = $state(new URLSearchParams(location.search).get("session") ?? "");

  const agent = new AgentSession<TicTacToeAgent>({
    get sessionId() {
      return sessionId;
    },
    transport: new HttpTransport({ baseUrl: API })
  });

  const board = $derived(agent.states.board); // the agent's `Board` model, kept current
  const play = (cell: number) => agent.sendMessage("play", { cell });
  const newGame = (agent_goes_first: boolean) => agent.sendMessage("new_game", { agent_goes_first });

  // Creating a session is the harness API, not the agent's: the one call made without `agent`.
  let startError = $state<string | null>(null);
  async function start(agentGoesFirst: boolean): Promise<void> {
    if (!sessionId) {
      const response = await fetch(`${API}sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_workflow_type: "TicTacToeAgent" })
      }).catch((error: Error) => error);
      if (!(response instanceof Response) || !response.ok) {
        startError = `Cannot create a session at ${API} — are \`just server\` and \`just worker\` running?`;
        return;
      }
      sessionId = ((await response.json()) as { workflow_id: string }).workflow_id;
      history.replaceState(null, "", `?session=${sessionId}`);
      startError = null;
    }
    await newGame(agentGoesFirst);
  }

  // What the TypeSafe tool returned (`SystemOneResult` in models.py). Tool results are opaque to
  // the generated types, so this is the page's own reading of it.
  interface Judgment {
    model: string;
    input_tokens: number | null;
    output_tokens: number | null;
    nouls: Record<string, number>;
    choices: Record<string, { choice: string; confidence: number; probabilities: Record<string, number> }>;
    scores: Record<string, { score: number }>;
  }
  const judgment = $derived.by(() => {
    const calls = agent.messages.flatMap((m) => m.parts).filter((p) => p.type === "tool");
    const last = calls.findLast((p) => p.toolName === "typesafe_system_one" && p.state === "done");
    return last?.output ? (JSON.parse(last.output) as Judgment) : null;
  });
  const move = $derived(judgment?.choices.move);

  const busy = $derived(agent.agentStatus === "busy" || agent.pending.some((m) => m.status === "sending"));
  const you = $derived(board && (board.agent_mark === "X" ? "O" : "X"));
  const yourTurn = $derived(board?.status === "playing" && board.to_move === you && !busy);
  // Your move shows the moment you click: a `play` still in flight draws your mark on its cell
  // until the board's own patch for it lands (or the send fails and it disappears).
  const playing = $derived(
    [...agent.pending, ...agent.messages].flatMap((m) =>
      m.handler === "play" && m.status !== "done" && m.status !== "error" ? [m.input.cell] : []
    )
  );
  const cells = $derived(board?.cells.map((mark, i) => mark ?? (playing.includes(i + 1) ? you : null)));

  const LINES:[number, number, number][] = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [0, 3, 6], [1, 4, 7], [2, 5, 8], [0, 4, 8], [2, 4, 6]];
  const win = $derived(board && LINES.find(([a, b, c]) => board.cells[a] && board.cells[a] === board.cells[b] && board.cells[b] === board.cells[c]));

  const percent = (value: number) => `${Math.round(value * 100)}%`;

  // The ledger: the root agent's frames as they land, newest first.
  const ledger = $derived(
    agent.frames.flatMap(({ event, data }) => {
      if (event === "stream_error") return [{ kind: "error", label: data.kind, body: data.message, at: Date.now() / 1000 }];
      if (data.agent_id !== agent.rootAgentId) return [];
      const at = data.timestamp;
      switch (event) {
        case "state_snapshot":
          return [{ kind: "patch", label: `snapshot v${data.version}`, body: JSON.stringify(data.value), at }];
        case "state_patch":
          return [{ kind: "patch", label: `patch v${data.version}`, body: data.ops.map((o) => `${o.op} ${o.path}${"value" in o ? ` ${JSON.stringify(o.value)}` : ""}`).join("\n"), at }];
        case "tool_start": {
          const typesafe = data.tool_name === "typesafe_system_one" && (data.tool_input as { request: { model: string; questions: object } }).request;
          const body = typesafe ? `asking ${Object.keys(typesafe.questions).join(", ")} (${typesafe.model})` : JSON.stringify(data.tool_input);
          return [{ kind: "tool", label: `${data.tool_name} →`, body, at }];
        }
        case "tool_end": {
          const pick = data.tool_name === "typesafe_system_one" && (JSON.parse(data.tool_output) as Judgment).choices.move;
          const top = Object.entries(pick ? pick.probabilities : {}).sort((a, b) => b[1] - a[1]).slice(0, 3);
          const body = pick
            ? `${pick.choice} @ ${percent(pick.confidence)} · top ${top.map(([cell, p]) => `${cell.slice(5)}:${percent(p)}`).join(" ")}`
            : data.tool_output.split("\n")[0]!;
          return [{ kind: "tool", label: `${data.tool_name} ←`, body, at }];
        }
        case "tool_error":
          return [{ kind: "error", label: `${data.tool_name} ✕`, body: data.message, at }];
        case "message_handler_end":
          return [{ kind: "reply", label: "agent", body: String(data.output.text ?? "").replace(/```[\s\S]*?```\n?/g, "").trim(), at }];
        case "message_handler_error":
          return [{ kind: "error", label: "handler", body: data.message, at }];
        default:
          return [];
      }
    }).reverse()
  );
</script>

<svelte:window
  onkeydown={(e) => /^[1-9]$/.test(e.key) && yourTurn && !cells?.[Number(e.key) - 1] && play(Number(e.key))}
/>

<header>
  <h1>Tic-Tac-Toe <small>a board drawn from observable state</small></h1>
  {#if sessionId}
    <span class="conn" data-state={agent.connection}>{agent.connection} · {sessionId.slice(0, 18)}</span>
    <a href="?">new session</a>
  {/if}
</header>

{#snippet newGameButtons(disabled: boolean)}
  <div class="row">
    <button class="primary" {disabled} onclick={() => start(false)}>New game — you open</button>
    <button {disabled} onclick={() => start(true)}>New game — agent opens</button>
  </div>
{/snippet}

{#if !sessionId}
  <!-- Nothing reads `agent` until there is a session: reading it is what opens the connection. -->
  <section class="start">
    <p>Play the TypeSafe tic-tac-toe agent. You are X unless it opens.</p>
    {@render newGameButtons(false)}
    {#if startError}<div class="notice error">{startError}</div>{/if}
  </section>
{:else}
<main>
  <section>
    <div class="status">
      {#if !board}
        Waiting for the board…
      {:else if board.status === "draw"}
        Draw.
      {:else if board.status !== "playing"}
        <b>{board.status[0]}</b> wins — {board.status[0] === board.agent_mark ? "the agent" : "you"}.
      {:else if busy}
        Agent is judging…
      {:else}
        {yourTurn ? "Your move" : "Agent to move"} <b>({board.to_move})</b>
      {/if}
    </div>

    <div class="board">
      {#each cells ?? Array(9).fill(null) as mark, i (i)}
        {@const p = mark ? undefined : move?.probabilities[`cell_${i + 1}`]}
        <button
          class="cell"
          class:win={win?.includes(i)}
          class:picked={move?.choice === `cell_${i + 1}`}
          style:--p={p}
          data-mark={mark}
          disabled={!!mark || !yourTurn}
          onclick={() => play(i + 1)}
        >
          {#key mark}<span in:scale={{ start: 0.4, duration: 250 }}>{mark ?? i + 1}</span>{/key}
          {#if p !== undefined}<small>{percent(p)}</small>{/if}
        </button>
      {/each}
    </div>

    {@render newGameButtons(busy)}

    {#each agent.pendingApprovals as { part } (part.toolId)}
      <div class="notice">
        <b>{part.toolName}</b> is waiting for your approval.
        <div class="row">
          <button class="primary" onclick={() => agent.respondToApproval(part.toolId, { approved: true })}>Approve</button>
          <button onclick={() => agent.respondToApproval(part.toolId, { approved: false })}>Deny</button>
        </div>
      </div>
    {/each}
    {#each agent.pending.filter((m) => m.status === "error") as failed (failed.id)}
      <div class="notice error">
        Could not send <code>{failed.handler}</code>: {failed.error}
        <button onclick={() => agent.dismiss(failed.id)}>Dismiss</button>
      </div>
    {/each}
    {#if agent.error}
      <div class="notice error">
        {agent.error.message}
        <button onclick={() => agent.clearError()}>Dismiss</button>
      </div>
    {/if}

    {#if judgment}
      <dl class="judgment">
        <dt>last judgment</dt>
        {#if move}<dd><span>move · Choice</span><b>{move.choice.replace("_", " ")} @ {percent(move.confidence)}</b></dd>{/if}
        {#if judgment.nouls.threat !== undefined}<dd><span>threat · Noul</span><b>{percent(judgment.nouls.threat)}</b></dd>{/if}
        {#if judgment.scores.outlook}
          {@const score = judgment.scores.outlook.score}
          <dd><span>outlook · Score</span><b>{score.toFixed(2)} {["losing", "even", "winning"][Math.round(Math.min(2, Math.max(0, score)))]}</b></dd>
        {/if}
        <dd><span>{judgment.model}</span><b>{judgment.input_tokens ?? "?"} in · {judgment.output_tokens ?? "?"} out</b></dd>
      </dl>
    {/if}
  </section>

  <section class="ledger">
    <h2>Ledger</h2>
    {#each ledger as entry, i (ledger.length - i)}
      <div class="entry" data-kind={entry.kind}>
        <span>{new Date(entry.at * 1000).toTimeString().slice(0, 8)}</span>
        <span class="label">{entry.label}</span>
        <span class="body">{entry.body}</span>
      </div>
    {:else}
      <div class="entry">{agent.connection === "connecting" ? "Replaying…" : "Nothing yet."}</div>
    {/each}
  </section>
</main>
{/if}
