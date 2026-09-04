<script lang="ts">
  import MetricStrip from "$lib/components/primitives/MetricStrip.svelte";
  import UsageLineChart from "$lib/components/flow/UsageLineChart.svelte";
  import ModelBreakdown from "$lib/components/flow/ModelBreakdown.svelte";
  import type { Metric } from "$lib/components/primitives/metrics";
  import {
    formatTokens,
    type CostSummary,
    type UsageTimelinePoint
  } from "$lib/cost/pricing";

  /**
   * The whole token reading — figures, chart, per-model split — with no opinion
   * about what is holding it.
   *
   * Split out of UsagePopover when tokens became a pane as well as a peek. The
   * two want the same reading and different shells: a popover is a glance that
   * costs no rearranging, a pane is a view that stays put while you scrub. What
   * they must never be is two copies of this markup — the figures overlap in ways
   * that need explaining (see `breakdown`), and an explanation that exists twice
   * is one that goes stale in one place.
   */
  interface Props {
    usage: CostSummary;
    usageTimeline: UsageTimelinePoint[];
    viewIndex: number;
    /**
     * The run's figures are unknown rather than zero, so show none of them.
     *
     * Every total below is a sum over the frames this console holds, and there
     * are runs it holds none of: a finished run whose event stream Temporal
     * cannot replay (`run.runUnmeasured`). An empty sum formatted as `0 tok`
     * reports a measurement that was never taken, beside runs whose zeros are
     * real.
     */
    unmeasured?: boolean;
  }

  let { usage, usageTimeline, viewIndex, unmeasured = false }: Props = $props();

  /* A dash says a figure is absent; it does not say why, and a reader who has only
     ever seen numbers here will read one as a broken panel. The full sentence is on
     the session banner already (`connectionError`), so this is the short form: what
     the dashes mean, and that nothing was lost. */
  const UNMEASURED_NOTE =
    "Unknown, not zero: this run finished and Temporal cannot replay its event stream, " +
    "so this console read none of the model calls it made. The run's own history is " +
    "intact in Temporal.";

  /** One figure, or the em dash that stands in for every figure of an unmeasured run. */
  function figure<T>(value: T, format: (value: T) => string): string {
    return unmeasured ? "—" : format(value);
  }

  /* How many tokens the run took, which is the whole reading for most openings
     of this panel. */
  const headline: Metric[] = $derived([
    {
      label: "total",
      value: figure(usage.tokens.total, formatTokens),
      /* The tone is affirmative — a total is drawn in --accent — and a dash has
         nothing to affirm. */
      tone: unmeasured ? "neutral" : "strong"
    }
  ]);

  /* Split off from the total rather than listed beside it, because these four
     OVERLAP each other and so cannot be added up — cached is a slice of input,
     and for most producers thought is a slice of output. Presented as a fifth
     peer of the total they read as addends, the sum comes to more than the
     total, and the total looks broken. It is not: it is the provider's own
     figure, and check-usage-totals.mjs pins it against exactly the arithmetic
     this separation is here to stop a reader attempting. */
  const breakdown: Metric[] = $derived([
    { label: "input", value: figure(usage.tokens.input, formatTokens) },
    { label: "output", value: figure(usage.tokens.output, formatTokens) },
    { label: "thought", value: figure(usage.tokens.thought, formatTokens) },
    { label: "cached", value: figure(usage.tokens.cached, formatTokens) }
  ]);

  const breakdownTip =
    "Each is part of the total, and they overlap: cached tokens are already inside " +
    "input, and reasoning tokens usually inside output. The total is the provider's " +
    "own figure, not the sum of these rows.";
</script>

<!-- Its own container, rather than measuring whichever box it landed in. A pane
     is resizable and starts at 400px where the popover is a fixed 720, so the two
     columns below genuinely need both forms — and an unnamed container query with
     no container above it does not fail loudly, it just never matches and leaves
     the two columns to overflow a narrow pane. One element buys immunity from
     whatever the host's CSS happens to be. It cannot be the grid itself: an
     element cannot answer its own container query. -->
<div class="usage-reading">
  <div class="usage-reading-body">
    <div class="usage-metrics">
      <!-- Every figure here is a sum up to the playhead, not for the whole run, and
           nothing else in a pane says so — the popover's header used to. Scrubbing
           moves them, so a reader who takes the total for the run's own is reading
           an answer to a question they did not ask. -->
      <p class="kicker usage-at-cursor">at cursor</p>
      <MetricStrip metrics={headline} dense />
      <p class="kicker usage-breakdown-label" data-tip={breakdownTip} data-tip-align="start">
        overlapping parts
      </p>
      <MetricStrip metrics={breakdown} dense />
    </div>
    <!-- A per-model split of nothing and a chart of a flat zero line are not
         hedged readings, they are empty boxes; the sentence that explains the
         dashes is the only thing this half of the panel has to say. -->
    {#if unmeasured}
      <p class="usage-unmeasured">{UNMEASURED_NOTE}</p>
    {:else}
      <!-- Stacked rather than a third peer column: the per-model split is a
           short list, and as a narrow column it had to shorten its own
           figures to fit. Under the chart it inherits the full width of this
           half and never has to. -->
      <div class="usage-detail">
        <UsageLineChart points={usageTimeline} {viewIndex} />
        <ModelBreakdown {usage} />
      </div>
    {/if}
  </div>
</div>

<style>
  .usage-reading {
    container-type: inline-size;
    min-width: 0;
    height: 100%;
    /* Its own scroller, because a pane body is `overflow: hidden` and hands its
       content no other way out: stacked into a 400px column the chart and the model
       list are taller than a short drawer, and clipped figures are worse than a
       scrollbar. Costs nothing where nothing overflows — a popover row is
       content-sized, so `height: 100%` resolves to auto there and never scrolls. */
    overflow: auto;
  }

  .usage-reading-body {
    min-width: 0;
    height: 100%;
    display: grid;
    grid-template-columns: minmax(190px, 0.8fr) minmax(300px, 1.4fr);
    gap: var(--gutter);
    /* Equal heights: boxes of different heights read as unrelated things rather
       than one reading of the same number. */
    align-items: stretch;
  }

  /* The chart takes the height the figures beside it happen to need; the model
     list takes what is left under it. */
  .usage-detail {
    min-width: 0;
    display: grid;
    grid-template-rows: minmax(0, 1fr) auto;
    gap: var(--gutter);
  }

  .usage-metrics {
    min-width: 0;
    display: grid;
    align-content: start;
    gap: var(--gutter-tight);
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-2);
  }

  .usage-at-cursor {
    margin: 0;
  }

  /* Sits between the two strips and does the separating: a rule the eye stops at
     before it reaches figures it must not add. Dotted underline because the rest
     of the sentence is on hover. */
  .usage-breakdown-label {
    margin: 0;
    padding-top: var(--gutter-tight);
    border-top: 1px solid var(--border);
    text-decoration: underline dotted var(--text-3);
    text-underline-offset: 3px;
    cursor: help;
  }

  /* Takes the track the model split and the chart have vacated, so the panel is
     one box of dashes and one sentence rather than a reading pushed into the
     left third of an otherwise empty 720px. */
  .usage-unmeasured {
    grid-column: 2;
    margin: 0;
    padding: var(--gutter);
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
  }

  /* Just above where the two minimums plus the gutter stop fitting. Was a
     viewport media query at 760px, which was the right answer while the popover
     was the only host: a 400px pane on a 1600px screen never fired it and the
     columns overflowed instead. */
  @container (max-width: 560px) {
    .usage-reading-body {
      grid-template-columns: minmax(0, 1fr);
      /* Stacked, the chart no longer has a tall neighbour to match, and stretching
         a two-row detail block over a pane's full height leaves the model list
         floating clear of its chart. */
      align-content: start;
      height: auto;
    }

    /* One track, so spanning from the second would invent an implicit column. */
    .usage-unmeasured {
      grid-column: 1;
    }
  }
</style>
