<script lang="ts">
  import { Cpu } from "@lucide/svelte";
  import { formatTokens, type CostSummary } from "$lib/cost/pricing";

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

<section class="model-breakdown" aria-label="Tokens by model">
  <div class="head">
    <Cpu size={15} />
    <span>Tokens by model</span>
  </div>

  {#if rows.length === 0}
    <div class="model-strip empty-state">
      <p class="empty">No model calls yet.</p>
    </div>
  {:else}
    <ul class="model-strip">
      {#each rows as row (row.model)}
        <li data-tip={`${formatTokens(row.tokens.input)} in · ${formatTokens(row.tokens.output)} out`}>
          <span class="name">{row.model}</span>
          <!-- A lone model spent every token in the run, so its bar is always
               full and its figure is the total already printed beside this card.
               Naming it is the whole reading; the rest is repetition. -->
          {#if rows.length > 1}
            <div class="row-meta">
              <span class="bar-track" aria-hidden="true">
                <span class="bar" style={`width: ${(row.tokens.total / maxTokens) * 100}%`}></span>
              </span>
              <span class="value">{formatTokens(row.tokens.total)}</span>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .model-breakdown {
    min-width: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    align-content: start;
    gap: var(--gutter-tight);
    /* Same inset as the metrics card and the chart beside it, so the three
       headings sit on one baseline instead of three. */
    padding: var(--gutter);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-2);
  }

  .head {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--text-2);
    font-size: var(--font-md);
    white-space: nowrap;
  }

  /* Wraps into as many tracks as fit rather than scrolling sideways. The
     scrolling version hid the second model entirely with no affordance, which
     is the same lie as a truncated number. */
  .model-strip {
    margin: 0;
    padding: 0;
    list-style: none;
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    align-content: start;
    gap: var(--gutter-tight) var(--gutter);
  }

  li {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: 4px;
  }

  .name {
    display: block;
    overflow: hidden;
    color: var(--text-1);
    font-size: var(--font-md);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* The bar takes whatever the figure leaves. The figure is never squeezed:
     a shortened token count is worse than no bar at all. */
  .bar-track {
    display: block;
    flex: 1;
    min-width: 0;
    height: 5px;
    border-radius: var(--radius-chip);
    background: color-mix(in srgb, var(--surface-0) 70%, transparent);
    overflow: hidden;
  }

  .bar {
    display: block;
    height: 100%;
    border-radius: var(--radius-chip);
    background: var(--model);
  }

  .row-meta {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .value {
    flex: none;
    color: var(--text-2);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .empty-state {
    display: flex;
    align-items: center;
  }

  .empty {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-sm);
  }
</style>
