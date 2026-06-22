<script lang="ts">
  import type { Metric } from "./metrics";

  interface Props {
    metrics: Metric[];
    dense?: boolean;
  }

  let { metrics, dense = false }: Props = $props();
</script>

<dl class={`metric-strip ${dense ? "dense" : ""}`}>
  {#each metrics as metric}
    <div class={`metric ${metric.tone ?? "neutral"}`}>
      <dt>{metric.label}</dt>
      <dd>{metric.value}</dd>
    </div>
  {/each}
</dl>

<style>
  .metric-strip {
    display: flex;
    align-items: stretch;
    gap: 8px;
    min-width: 0;
    margin: 0;
    overflow-x: auto;
  }

  .metric {
    min-width: 86px;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-2);
  }

  .dense .metric {
    min-width: 72px;
    padding: 6px 8px;
  }

  dt {
    margin: 0 0 3px;
    color: var(--text-3);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  dd {
    margin: 0;
    color: var(--text-1);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .strong dd { color: var(--accent); }
  .cost dd { color: var(--success); }
</style>
