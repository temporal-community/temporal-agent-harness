<script lang="ts">
  import Reading from "$lib/components/primitives/Reading.svelte";
  import UsageLineChart from "$lib/components/flow/UsageLineChart.svelte";
  import ModelBreakdown from "$lib/components/flow/ModelBreakdown.svelte";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import type { Metric } from "$lib/components/primitives/metrics";
  import {
    tokenFigure,
    UNREPORTED_TIP,
    type CostSummary,
    type UsageTimelinePoint
  } from "$lib/cost/pricing";
  import { buildStepTimeline, type StepTimeline } from "$lib/state/stepTimeline";
  import { categoryTotals, presentCategories, TOKEN_CATEGORY_STYLES, unreportedCategories } from "./usageChart";

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
    timeline?: StepTimeline;
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

  let { usage, usageTimeline, timeline = buildStepTimeline([]), viewIndex, unmeasured = false }: Props = $props();

  /* A dash says a figure is absent; it does not say why, and a reader who has only
     ever seen numbers here will read one as a broken panel. The full sentence is on
     the session banner already (`connectionError`), so this is the short form: what
     the dashes mean, and that nothing was lost. */
  const UNMEASURED_NOTE =
    "Unknown, not zero: this run finished and Temporal cannot replay its event stream, " +
    "so this console read none of the model calls it made. The run's own history is " +
    "intact in Temporal.";

  /* Same words, different cause: a call that ended with `usage: null` is not a
     replay failure, and saying it was would send the reader looking in Temporal. */
  const unreportedNote = $derived(
    `Unknown, not zero: ${usage.unreportedCalls === 1 ? "a model call" : `${usage.unreportedCalls} model calls`} ` +
      "ended without reporting token usage, so these count only the calls that did."
  );

  /* The known sum is still worth reading after a silent call, as long as it says
     it is a floor: "≥ 4,139,540" rather than a dash that throws the run away. */
  const lowerBound = $derived(!unmeasured && usage.unreportedCalls > 0);
  const note = $derived(lowerBound ? UNREPORTED_TIP : undefined);
  const CACHE_HIT_TIP =
    "Cached ÷ (input + cached), using the same provider cache field as the Cached figure. " +
    "Hidden when none report cache data; a lower bound when a call reports no usage.";
  const percent = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 0 });

  /** One figure: a dash for an unmeasured run, a lower bound after a silent call. */
  function figure(value: number): string {
    return unmeasured ? "—" : tokenFigure(value, usage.unreportedCalls);
  }

  /* How many tokens the run took, which is the whole reading for most openings
     of this panel. */
  const headline: Metric[] = $derived([
    {
      label: "total",
      value: figure(usage.tokens.total),
      /* The tone is affirmative — a total is drawn in --accent — and neither a
         dash nor a floor has anything to affirm. */
      tone: unmeasured || lowerBound ? "neutral" : "strong",
      note
    },
    ...(usage.cacheReadReportedCalls > 0
      ? [{
          label: "cache hit",
          value:
            unmeasured
              ? "—"
              : `${usage.cacheReadUnreportedCalls > 0 ? "≥ " : ""}${percent.format(
                  usage.tokens.cached / Math.max(1, usage.tokens.input + usage.tokens.cached)
                )}`,
          note: CACHE_HIT_TIP
        }]
      : [])
  ]);

  /* The protocol's fields overlap (cached is inside input, and for most producers
     thought is inside output), so they are shown as the chart's disjoint parts:
     each figure is one color of the bars, and the grid is their legend. Only
     categories the run spent tokens on are listed, from the whole run, so
     scrubbing never adds or drops a column. */
  const shareFormat = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });
  const categories = $derived(presentCategories(usageTimeline));
  const parts = $derived(categoryTotals(usageTimeline, viewIndex));
  const unreported = $derived(unreportedCategories(usageTimeline));
  const breakdown: Metric[] = $derived(
    categories.map((category) => {
      const { label, color } = TOKEN_CATEGORY_STYLES[category];
      const missing = unreported.get(category);
      const share = category === "decisions" && !unmeasured && usage.tokens.total > 0
        ? ` (${shareFormat.format(parts[category] / usage.tokens.total)})`
        : "";
      return {
        label,
        value: `${figure(parts[category])}${missing ? "*" : ""}${share}`,
        swatch: color,
        note: missing ? `${label}: not reported by ${missing.join(", ")}` : note
      };
    })
  );
  const footnotes = $derived(
    categories.flatMap((category) => {
      const missing = unreported.get(category);
      if (!missing) return [];
      const names = missing.slice(0, 3).join(", ") + (missing.length > 3 ? ` and ${missing.length - 3} more` : "");
      return [`* ${TOKEN_CATEGORY_STYLES[category].label}: not reported by ${names}`];
    })
  );

  const breakdownTip =
    "Disjoint parts of each call, one per bar color: input (uncached) is input less " +
    "cached, output leaves out thought where a provider counts it inside output, and " +
    "decisions are the auto-approval model's calls. They add up to the total unless a " +
    "provider counts tokens in its total that it reports nowhere else.";
</script>

<!-- Its own container, so a tip inside the reading measures this pane rather than
     the window. The figures are a header row at every width with the chart under
     them — the same place a latency rollup and an agent-state reading sit. A side column of those figures only grows once the pane is wider
     than they are, and the cell under them was empty. -->
<div class="usage-reading">
  {#if !unmeasured && usageTimeline.length === 0}
    <!-- Nothing has been metered yet, so there is no zero to report and no time
         to chart: a wall of zeros over an empty axis reads as a broken panel. -->
    <EmptyState title="No model calls yet">
      <p>Token counts and the chart appear after the agent's first model call.</p>
    </EmptyState>
  {:else}
    <div class="usage-reading-body">
      <div class="usage-metrics">
        <!-- Every figure here is a sum up to the playhead, not for the whole run, and
             nothing else in a pane says so. Scrubbing moves them, so a reader who takes
             the total for the run's own is reading an answer to a question they did
             not ask. -->
        <Reading label="At cursor" {headline} {breakdown} breakdownLabel="Breakdown" {breakdownTip} />
        {#each footnotes as footnote (footnote)}
          <p class="usage-footnote" dir="auto">{footnote}</p>
        {/each}
        {#if !unmeasured && usage.unreportedCalls > 0}
          <p class="usage-note" dir="auto">{unreportedNote}</p>
        {/if}
      </div>
      <!-- A per-model split of nothing and a chart of a flat zero line are not
           hedged readings, they are empty boxes; the sentence that explains the
           dashes is the only thing this half of the panel has to say. -->
      {#if unmeasured}
        <p class="usage-note usage-unmeasured" dir="auto">{UNMEASURED_NOTE}</p>
      {:else}
        <div class="usage-detail">
          <UsageLineChart points={usageTimeline} {timeline} {viewIndex} />
          <ModelBreakdown {usage} points={usageTimeline} {viewIndex} />
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .usage-reading {
    /* Tips inside measure this pane (`100cqi`), not the window. */
    container-type: inline-size;
    min-width: 0;
    height: 100%;
    box-sizing: border-box;
    /* Its own scroller, because a pane body is `overflow: hidden` and hands its
       content no other way out: stacked into a 400px column the chart and the model
       list are taller than a short drawer, and clipped figures are worse than a
       scrollbar. */
    overflow: auto;
    scrollbar-gutter: stable;
    padding: var(--pane-inset);
    padding-inline-end: calc(var(--pane-inset) - var(--scrollbar-size));
  }

  /* Figures, chart and model list sit on the pane itself, separated by space
     rather than by boxes: none of them is a control, and a border around a static
     number reads as something to press. */
  /* One column at every width: the figures are the header, then the chart at its
     own bounded height, then the model list. A second column put the figures at
     the top of a cell as tall as the chart, which is the empty well a full-screen
     pane was. The narrow pane already stacked this way; wide now does too. */
  .usage-reading-body {
    min-width: 0;
    min-height: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto 1fr;
    gap: calc(var(--gutter) * 2) var(--gutter);
  }

  /* A flex column; the chart keeps its own height: see .plot in UsageLineChart. */
  .usage-detail {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: calc(var(--gutter) * 1.5);
  }

  .usage-metrics {
    min-width: 0;
    align-self: start;
    display: grid;
    align-content: start;
    gap: var(--gutter-tight);
  }

  /* A model id has no spaces to break at, so it breaks anywhere rather than overflow. */
  .usage-footnote {
    margin: 0;
    color: var(--text-3);
    font-size: var(--font-xs);
    overflow-wrap: anywhere;
  }

  .usage-note {
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.4;
  }
</style>
