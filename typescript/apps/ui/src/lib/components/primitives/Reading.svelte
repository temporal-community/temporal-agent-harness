<script lang="ts">
  import MetricStrip from "./MetricStrip.svelte";
  import type { Metric } from "./metrics";

  /**
   * A pane's headline reading: what it is a reading of, the figure it leads with, and
   * the figures that qualify it. Tokens, State and Latency open with this, so their
   * headers read as one instrument. Nothing here is a control, so nothing is boxed.
   *
   * The qualifying figures share the lead's row. A second strip puts that lead on a
   * row by itself — a narrow pane was drawing version above commits, as if the two
   * were different instruments. A breakdown label is the exception: it names a
   * different reading, parts that overlap the total, and those stay under it.
   */
  interface Props {
    /** What the figures are a reading of. One line; the whole of it is in the tip. */
    label: string;
    headline: Metric[];
    breakdown?: Metric[];
    breakdownLabel?: string;
    breakdownTip?: string;
  }

  let { label, headline, breakdown = [], breakdownLabel, breakdownTip }: Props = $props();

  const peers = $derived(breakdownLabel ? headline : [...headline, ...breakdown]);
  const parts = $derived(breakdownLabel ? breakdown : []);
</script>

<div class="reading">
  <p class="kicker reading-label" title={label} dir="auto">{label}</p>
  {#if peers.length > 0}
    <MetricStrip metrics={peers} bare />
  {/if}
  {#if breakdownLabel}
    <!-- Focusable when it carries a tip: the tip shows on hover and on :focus-visible. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <p
      class="kicker reading-breakdown-label"
      tabindex={breakdownTip ? 0 : undefined}
      data-tip={breakdownTip}
      data-tip-align={breakdownTip ? "start" : undefined}
    >
      {breakdownLabel}
    </p>
  {/if}
  {#if parts.length > 0}
    <MetricStrip metrics={parts} dense bare grid={parts.some((part) => part.swatch)} />
  {/if}
</div>

<style>
  .reading {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: var(--gutter-tight);
  }

  .reading > p {
    margin: 0;
  }

  .reading-label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .reading-breakdown-label {
    justify-self: start;
    margin-top: var(--gutter-tight) !important;
  }
</style>
