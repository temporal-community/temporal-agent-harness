<!-- The design doc's goal, as a component svelte-check type-checks against the example's
     generated client types. It is never rendered; it is here to be compiled. -->
<script lang="ts">
  import type { TicTacToeAgent } from "../../../examples/tictactoe/client_sdk/TicTacToeAgent";
  import { AgentSession } from "../src/lib/index.js";

  let { sessionId }: { sessionId: string } = $props();
  const agent = new AgentSession<TicTacToeAgent>({
    get sessionId() {
      return sessionId;
    }
  });
  const board = $derived(agent.states.board);

  export function misuse(): void {
    // @ts-expect-error a move names a cell
    void agent.sendMessage("play", {});
    // @ts-expect-error the agent has no such handler
    void agent.sendMessage("resign", {});
  }
</script>

{#if board}
  {#each board.cells as cell, i (i)}
    <button
      disabled={cell !== null || board.status !== "playing"}
      onclick={() => agent.sendMessage("play", { cell: i + 1 })}>{cell ?? ""}</button
    >
  {/each}
{/if}

{#each agent.messages as message (message.id)}
  {#if message.handler === "play"}
    <p>You played {message.input.cell}: {message.output?.text ?? "…"}</p>
  {/if}
{/each}
