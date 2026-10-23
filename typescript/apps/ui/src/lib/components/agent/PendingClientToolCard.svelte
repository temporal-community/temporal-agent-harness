<script lang="ts">
  import { RotateCw } from "@lucide/svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import type { ClientToolStatus } from "$lib/state/clientToolProvider";
  import type { ReplayLogRow } from "$lib/state/replayLog";
  import PendingCard from "./PendingCard.svelte";

  interface Props {
    row: ReplayLogRow;
    status: ClientToolStatus;
    ahead?: boolean;
    onRetry?: () => void;
  }

  let { row, status: toolStatus, ahead = false, onRetry }: Props = $props();
  const title = $derived(row.toolName || "Client tool");
</script>

<PendingCard {row} {title} label={`Client tool: ${title}, answered by the console`} {ahead}>
  {#snippet status()}
    {#if toolStatus.state === "failed"}
      <p class="error" role="alert">{toolStatus.error}</p>
    {:else}
      <p class="note" role="status">
        {toolStatus.state === "answering"
          ? "Answering automatically…"
          : "Answered by console · waiting for the agent to continue"}
      </p>
    {/if}
  {/snippet}
  {#snippet foot()}
    {#if toolStatus.state === "failed"}
      <Chip fill="quiet" dense label="Retry" aria-label="Retry" data-tip="Retry" onclick={onRetry}>
        {#snippet lead()}<RotateCw size={12} />{/snippet}
      </Chip>
    {/if}
  {/snippet}
</PendingCard>

<style>
  .note,
  .error {
    margin: 0;
    font-size: var(--font-sm);
  }

  .note {
    color: var(--text-3);
  }

  .error {
    max-height: 6lh;
    overflow-y: auto;
    color: var(--error);
    overflow-wrap: anywhere;
  }
</style>
