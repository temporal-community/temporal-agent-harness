<script lang="ts">
  import { Cpu } from "@lucide/svelte";
  import { formatCost, formatTokens, type CostSummary } from "$lib/cost/pricing";

  interface Props {
    usage: CostSummary;
  }

  let { usage }: Props = $props();

  const rows = $derived(
    [...usage.modelBreakdown].sort((a, b) => b.tokens.total - a.tokens.total)
  );
  const maxTokens = $derived(
    rows.reduce((max, row) => Math.max(max, row.tokens.total), 1)
  );
</script>

<section class="model-breakdown" aria-label="Cost by model">
  <div class="head">
    <Cpu size={15} />
    <span>Cost by model</span>
  </div>

  {#if rows.length === 0}
    <div class="model-strip empty-state">
      <p class="empty">No model calls yet.</p>
    </div>
  {:else}
    <ul class="model-strip">
      {#each rows as row}
        <li>
          <div class="row-top">
            <span class="name">{row.model}</span>
            <span class="cost">{formatCost(row.estimatedCostUsd)}</span>
          </div>
          <div class="bar-track" aria-hidden="true">
            <span class="bar" style={`width: ${(row.tokens.total / maxTokens) * 100}%`}></span>
          </div>
          <div class="row-meta">
            <span>{formatTokens(row.tokens.total)} tok</span>
            <span>{formatTokens(row.tokens.input)} in · {formatTokens(row.tokens.output)} out</span>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .model-breakdown {
    min-width: 0;
    height: 88px;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    align-content: start;
    gap: 7px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
  }

  .head {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-2);
    font-size: 12px;
    white-space: nowrap;
  }

  .model-strip {
    margin: 0;
    padding: 0;
    list-style: none;
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-auto-flow: column;
    grid-auto-columns: minmax(154px, 1fr);
    gap: 8px;
    overflow-x: auto;
    overflow-y: hidden;
    scrollbar-width: thin;
  }

  li {
    min-width: 0;
    display: grid;
    align-content: start;
    padding-right: 8px;
    border-right: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  }

  li:last-child {
    border-right: 0;
  }

  .row-top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
  }

  .name {
    overflow: hidden;
    color: var(--text-1);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cost {
    color: var(--success);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .bar-track {
    height: 5px;
    margin: 5px 0 4px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--surface-0) 70%, transparent);
    overflow: hidden;
  }

  .bar {
    display: block;
    height: 100%;
    border-radius: 999px;
    background: var(--model);
  }

  .row-meta {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    overflow: hidden;
    color: var(--text-3);
    font-size: 10px;
    font-variant-numeric: tabular-nums;
  }

  .row-meta span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .empty-state {
    display: flex;
    align-items: center;
  }

  .empty {
    margin: 0;
    color: var(--text-3);
    font-size: 11px;
  }
</style>
