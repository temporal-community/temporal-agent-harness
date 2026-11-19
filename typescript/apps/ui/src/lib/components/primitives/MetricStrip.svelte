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
    /** Figures without their boxes, for a strip inside a card that already has one. */
    bare?: boolean;
    /** Equal columns, so figures that key a chart line up as one legend. */
    grid?: boolean;
  }

  let { metrics, dense = false, bare = false, grid = false }: Props = $props();
</script>

<!-- A list of "label value" items, not a <dl>: a description list allows nothing but dt and dd
     in a group, and the icon and swatch sit beside the pair. -->
<div class={["metric-strip", dense && "dense", bare && "bare", grid && "grid"]} role="list">
  {#each metrics as metric (metric.label)}
    <!-- A note is a tip the keyboard can reach, not a `title` that waits a second for a resting pointer. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div
      class={`metric ${metric.tone ?? "neutral"}`}
      role="listitem"
      data-tip={metric.note}
      data-tip-align={metric.note ? "start" : undefined}
      tabindex={metric.note ? 0 : undefined}
    >
      {#if metric.icon}
        <span
          class="metric-icon"
          style={metric.hue ? `color: var(${metric.hue})` : undefined}
          aria-hidden="true"
        >
          <metric.icon size={14} />
        </span>
      {/if}
      {#if metric.swatch}<span class="metric-swatch" style:background={metric.swatch} aria-hidden="true"></span>{/if}
      <div class="metric-text">
        <span class="kicker metric-label">{metric.label}</span>
        <span class="metric-value">{metric.value}</span>
      </div>
    </div>
  {/each}
</div>

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

  /* With no box, nothing pads one figure off the next, so the strip spaces them. */
  .bare {
    column-gap: var(--gutter);
  }

  .bare .metric {
    min-width: 0;
    padding: 0;
    border: 0;
    background: none;
  }

  .metric-icon {
    display: inline-flex;
    flex: none;
  }

  /* Two columns at a 320px pane, three from about 430px; a label wraps rather than clips. */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 8.5rem), 1fr));
    row-gap: var(--gutter-tight);
  }

  .grid .metric {
    align-items: flex-start;
  }

  .grid .metric-label {
    white-space: normal;
  }

  /* The legend's square, beside the figure rather than the label, at the label's size. */
  .metric-swatch {
    flex: none;
    width: 10px;
    height: 10px;
    margin-top: 0.35em;
  }

  /* Value on top, label under it. Reversed in CSS rather than the DOM so a screen
     reader reads the label first. */
  .metric-text {
    min-width: 0;
    display: flex;
    flex-direction: column-reverse;
    align-items: flex-start;
    line-height: 1.2;
  }

  /* A figure is a number in any language. Isolated left to right, or a right-to-left
     page mirrors "≥" into "≤" and reverses a lower bound into an upper one. */
  .metric-value {
    direction: ltr;
    unicode-bidi: isolate;
    font-family: var(--font-mono);
    color: var(--text-1);
    font-size: var(--font-lg);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .strong .metric-value { color: var(--accent); }
  .cost .metric-value { color: var(--success); }
  .pending .metric-value { color: var(--text-3); }
  .quiet .metric-value { color: var(--text-2); }
</style>
