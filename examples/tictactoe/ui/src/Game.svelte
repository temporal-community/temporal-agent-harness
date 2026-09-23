<!-- The board, drawn only from the agent's `board` state; the controls; the last judgment; and
     anything waiting on this client. -->
<script lang="ts">
  import { AgentSession, type SessionTransport } from "@temporal-agent-harness/svelte";

  import type { TicTacToeAgent } from "../../client_sdk/TicTacToeAgent";
  import { lastJudgment, other, percent, winningLine } from "./game";

  let { sessionId, transport }: { sessionId: string; transport: SessionTransport } = $props();

  const agent = new AgentSession<TicTacToeAgent>({
    get sessionId() {
      return sessionId;
    },
    // svelte-ignore state_referenced_locally
    transport
  });

  const board = $derived(agent.states.board);
  const busy = $derived(
    agent.agentStatus === "busy" || agent.pending.some((m) => m.status === "sending")
  );
  const you = $derived(board ? other(board.agent_mark) : null);
  const yourTurn = $derived(!!board && board.status === "playing" && board.to_move === you && !busy);
  const win = $derived(board ? winningLine(board.cells) : null);
  const judgment = $derived(lastJudgment(agent.messages));
  const move = $derived(judgment?.choices.move ?? null);
  const failedSends = $derived(agent.pending.filter((m) => m.status === "error"));

  // Cells that changed in the last update of the board, and a generation that re-mounts them
  // so they flash. Recomputed only when the board changes; `previous` is deliberately not state.
  let previous: readonly (string | null)[] | null = null;
  let generation = 0;
  const flashes = $derived.by(() => {
    const cells = board?.cells ?? null;
    const changed = new Set<number>();
    if (cells && previous) cells.forEach((cell, i) => cell !== previous![i] && changed.add(i));
    previous = cells ? [...cells] : null;
    if (changed.size > 0) generation += 1;
    return { changed, generation };
  });

  function play(cell: number): void {
    if (yourTurn) void agent.sendMessage("play", { cell });
  }

  function newGame(agentGoesFirst: boolean): void {
    void agent.sendMessage("new_game", { agent_goes_first: agentGoesFirst });
  }

  function onKeydown(event: KeyboardEvent): void {
    const target = event.target as HTMLElement | null;
    if (/^[1-9]$/.test(event.key) && target?.tagName !== "INPUT") play(Number(event.key));
  }

  function outlookLabel(score: number): string {
    return ["losing", "even", "winning"][Math.round(Math.min(2, Math.max(0, score)))]!;
  }
</script>

<svelte:window onkeydown={onKeydown} />

<section>
  <div class="status">
    <div class="who">
      {#if !board}
        {agent.connection === "idle" ? "No board yet — start a game." : "Waiting for the board…"}
      {:else if board.status === "draw"}
        Draw.
      {:else if board.status !== "playing"}
        {@const winner = board.status[0]}
        <span class="mark">{winner}</span> wins — {winner === board.agent_mark ? "the agent" : "you"}.
      {:else if busy}
        Agent is judging…
      {:else if board.to_move === you}
        Your move <span class="mark">({you})</span>
      {:else}
        Agent to move <span class="mark">({board.agent_mark})</span>
      {/if}
    </div>
    <div class="meta">
      <span class="conn">
        <span
          class="dot"
          data-state={agent.connection === "error"
            ? "error"
            : busy
              ? "busy"
              : agent.connection === "live" || agent.connection === "idle"
                ? "live"
                : ""}
        ></span>
        {agent.connection}
      </span>
      {#if board}
        · {board.moves.length} move{board.moves.length === 1 ? "" : "s"}
      {/if}
    </div>
  </div>

  <div class="board" role="grid" aria-label="tic-tac-toe board">
    {#each board?.cells ?? Array(9).fill(null) as mark, i (`${i}:${flashes.changed.has(i) ? flashes.generation : 0}`)}
      {@const key = `cell_${i + 1}`}
      {@const p = move?.probabilities[key]}
      {@const weighed = p !== undefined && (!mark || key === move?.choice)}
      <button
        type="button"
        role="gridcell"
        class="cell"
        class:playable={!mark && yourTurn}
        class:win={win?.includes(i)}
        class:changed={flashes.changed.has(i)}
        class:heat={weighed && !mark}
        class:picked={weighed && key === move?.choice}
        style:--w={weighed ? p : undefined}
        data-mark={mark ?? undefined}
        disabled={!!mark || !yourTurn}
        aria-label={`cell ${i + 1}, ${mark ?? "empty"}`}
        onclick={() => play(i + 1)}
      >
        {mark ?? i + 1}
        {#if weighed}<span class="p">{percent(p)}</span>{/if}
      </button>
    {/each}
  </div>

  <div class="controls">
    <button class="btn primary" disabled={busy} onclick={() => newGame(false)}>New game — you open</button>
    <button class="btn" disabled={busy} onclick={() => newGame(true)}>New game — agent opens</button>
  </div>

  {#each agent.pendingApprovals as waiting (waiting.part.toolId)}
    <div class="notice">
      <span><b>{waiting.part.toolName}</b> is waiting for your approval.</span>
      <div class="actions">
        <button class="btn primary" onclick={() => agent.respondToApproval(waiting.part.toolId, { approved: true })}>
          Approve
        </button>
        <button class="btn" onclick={() => agent.respondToApproval(waiting.part.toolId, { approved: false })}>
          Deny
        </button>
      </div>
    </div>
  {/each}

  {#if agent.error}
    <div class="notice error">
      <span>{agent.error.message}</span>
      <div class="actions"><button class="btn" onclick={() => agent.clearError()}>Dismiss</button></div>
    </div>
  {/if}
  {#each failedSends as sent (sent.id)}
    <div class="notice error">
      <span>Could not send <code>{sent.handler}</code>: {sent.error}</span>
      <div class="actions"><button class="btn" onclick={() => agent.dismiss(sent.id)}>Dismiss</button></div>
    </div>
  {/each}

  {#if judgment}
    {@const threat = judgment.nouls.threat}
    {@const outlook = judgment.scores.outlook}
    <div class="judgment">
      <div class="k">last judgment</div>
      {#if move}
        <div class="row"><span>move · Choice</span><b>{move.choice.replace("cell_", "cell ")} @ {percent(move.confidence)}</b></div>
      {/if}
      {#if threat !== undefined}
        <div class="row"><span>threat · Noul</span><b>{percent(threat)}</b></div>
      {/if}
      {#if outlook}
        <div class="row"><span>outlook · Score</span><b>{outlook.score.toFixed(2)} {outlookLabel(outlook.score)}</b></div>
      {/if}
      <div class="row"><span>{judgment.model}</span><b>{judgment.input_tokens ?? "?"} in · {judgment.output_tokens ?? "?"} out</b></div>
    </div>
  {/if}

  <p class="hint">
    Click an empty cell or press 1–9 to play. The board never moves on its own here: it is the
    agent's <code>board</code> state, typed from its Python model, as the session's event stream
    delivers it.
  </p>
</section>
