<script lang="ts">
  import type { Metric } from "./metrics";

  /**
   * The one way to show a number with a name under it. Used for token totals,
   * per-kind latency rollups, and anything else that would otherwise grow its
   * own little card.
   */
  interface Props {
    metrics: Metric[];
    dense?: boolean;
  }

  let { metrics, dense = false }: Props = $props();
</script>

<dl class={`metric-strip ${dense ? "dense" : ""}`}>
  {#each metrics as metric}
    <div class={`metric ${metric.tone ?? "neutral"}`} title={metric.note}>
      {#if metric.icon}
        <span
          class="metric-icon"
          style={metric.hue ? `color: var(${metric.hue})` : undefined}
          aria-hidden="true"
        >
          <metric.icon size={14} />
        </span>
      {/if}
      <div class="metric-text">
        <dt class="kicker">{metric.label}</dt>
        <dd>{metric.value}</dd>
      </div>
    </div>
  {/each}
</dl>

<style>
  /* Wraps rather than scrolls. A scroll region with no visible scrollbar reads
     as truncated data, and a half-shown figure is worse than a taller box. */
  .metric-strip {
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: var(--gutter-tight);
    min-width: 0;
    margin: 0;
  }

  .metric {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-width: 86px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    background: var(--surface-2);
  }

  .dense .metric {
    min-width: 72px;
    padding: 6px 8px;
  }

  .metric-icon {
    display: inline-flex;
    flex: none;
  }

  /* Value on top, label under it. Reversed in CSS rather than the DOM so the
     definition list keeps its term-before-description order. */
  .metric-text {
    min-width: 0;
    display: flex;
    flex-direction: column-reverse;
    align-items: flex-start;
    line-height: 1.2;
  }

  dt {
    margin: 0;
  }

  dd {
    font-family: var(--font-mono);
    margin: 0;
    color: var(--text-1);
    font-size: var(--font-lg);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .strong dd { color: var(--accent); }
  .cost dd { color: var(--success); }
</style>
