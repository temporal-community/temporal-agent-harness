<script lang="ts">
  import { Chart } from "@tanstack/charts/svelte";
  import { SvelteSet } from "svelte/reactivity";
  import TipCard, { type TipField } from "$lib/components/primitives/TipCard.svelte";
  import { formatTokens, usagePointAt, type UsageTimelinePoint } from "$lib/cost/pricing";
  import { formatDuration, formatElapsedDuration } from "$lib/state/replayLog";
  import { buildStepTimeline, timestampAtIndex, type StepTimeline } from "$lib/state/stepTimeline";
  import {
    categoryTotals,
    drawsTotal,
    hoveredActivity,
    hoveredSlot,
    LEFT_AXIS_MIN,
    lineStyle,
    lineSwatch,
    metricIcon,
    modelKey,
    presentCategories,
    slotBudget,
    slotTipFields,
    slotXAt,
    TOKEN_CATEGORY_STYLES,
    TOTAL_LINE_STYLE,
    usageActivity,
    usageChart,
    usageGaps,
    usageSlots,
    waitedSeconds,
    type SeriesKey,
    type UsageActivity,
    type UsageMetric,
    type UsageSlot
  } from "./usageChart";

  interface Props {
    /** One point per model call, from the run's incremental usage fold. */
    points: UsageTimelinePoint[];
    timeline?: StepTimeline;
    viewIndex: number;
  }

  let { points, timeline = buildStepTimeline([]), viewIndex }: Props = $props();

  /* The plot's height range. It follows the pane's width between the two, never its
     height: a plot stretched to a tall pane draws four calls as cliffs. */
  const PLOT_MIN = 140;
  const PLOT_MAX = 260;
  const compactTokens = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
  const compactRate = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

  /* The renderer sizes its own SVG from the number it is handed, so the box is
     measured and the number passed down. The box is sized by CSS, never by the SVG
     (which sits absolutely inside it), so the two cannot feed each other. The width
     sets how many bars fit. */
  let plotBox = $state(0);
  let plotWidth = $state(0);
  const plotHeight = $derived(Math.min(PLOT_MAX, Math.max(PLOT_MIN, Math.round(plotBox))));

  /* The whole run sizes the axis and names the legend, and it is rebinned only
     when a call lands or the plot changes width. Scrubbing re-cuts the series
     at the cursor — the same slots, without the calls it has not reached — and
     leaves the axis where it is. At the live edge the cut is the run itself. */
  const budget = $derived(slotBudget(plotWidth));
  const run = $derived(usageSlots(points, budget));
  const atEnd = $derived(points.at(-1) != null && viewIndex >= points.at(-1)!.index);
  const layout = $derived(run.slots.length === 0 || atEnd ? run : usageSlots(points, budget, viewIndex));
  const origin = $derived(points.reduce((first, point) => Math.min(first, point.timestamp), Infinity));
  const cursorCall = $derived(usagePointAt(points, viewIndex));
  const cursor = $derived(cursorCall ? { x: slotXAt(layout.slots, Math.max(0, cursorCall.timestamp - origin)) } : undefined);
  /* Seconds from the first call. Absent at the live edge, so a finished wait is
     not trimmed by a clock that has moved on past the run. */
  const playheadTs = $derived(timestampAtIndex(timeline.workflow.points, viewIndex));
  const cursorSeconds = $derived(
    atEnd || playheadTs == null || !Number.isFinite(origin) ? undefined : Math.max(0, playheadTs - origin)
  );
  const activity = $derived(usageActivity(timeline, origin, run.span));
  const gaps = $derived(usageGaps(timeline, origin));
  const reducedMotion = prefersReducedMotion();
  /* Per chart, for as long as the pane is open: a legend entry switches its series. */
  const hidden = new SvelteSet<SeriesKey>();
  const toggle = (key: SeriesKey) => (hidden.has(key) ? hidden.delete(key) : hidden.add(key));
  /* A copy, so the chart re-derives on any key; `has` alone tracks only the keys it read. */
  const definition = $derived(
    usageChart({
      layout,
      domain: run,
      cursor,
      viewIndex,
      cursorSeconds,
      activity,
      gaps,
      plotHeight,
      reducedMotion,
      hidden: new Set(hidden)
    })
  );

  /* Read once: a replay footer is not a place to re-run layout on a media change. */
  function prefersReducedMotion(): boolean {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  const calls = $derived(points.length);
  const categories = $derived(presentCategories(points));
  const cursorParts = $derived(categoryTotals(points, viewIndex));
  /* The bars' own colors, bottom to top, so the switch shows what it hides. */
  const callsSwatch = $derived(
    categories.length === 0
      ? undefined
      : `linear-gradient(0deg, ${categories
          .map((category, k) => `${TOKEN_CATEGORY_STYLES[category].color} ${(k / categories.length) * 100}% ${((k + 1) / categories.length) * 100}%`)
          .join(", ")})`
  );
  const cursorOrdinal = $derived(cursorCall ? points.indexOf(cursorCall) + 1 : 0);
  /* Past one call per few pixels a bar is the calls in its stretch of time, and the
     legend has to stop calling it one call. */
  const grouped = $derived(run.downsampled);
  const unknownCalls = $derived(run.slots.at(-1)?.unknownSoFar ?? 0);
  const finalTotal = $derived(run.slots.at(-1)?.running ?? 0);
  const showActivity = $derived(plotWidth >= LEFT_AXIS_MIN);
  const showTotal = $derived(drawsTotal(run));
  const hasTools = $derived(showActivity && activity.some((item) => item.kind === "tool"));
  const waited = $derived(showActivity ? waitedSeconds(activity) : 0);
  const hasWaits = $derived(showActivity && activity.some((item) => item.kind === "wait"));
  const legendKeys = $derived<SeriesKey[]>([
    ...(calls > 0 ? ["calls" as const] : []),
    ...(showTotal ? ["total" as const] : []),
    ...run.lines.map(modelKey),
    ...(hasTools ? ["tools" as const] : []),
    ...(hasWaits ? ["waits" as const] : [])
  ]);
  const allHidden = $derived(legendKeys.length > 0 && legendKeys.every((key) => hidden.has(key)));

  function visibleModel(model: string): string {
    return model.replaceAll("\n", "↵").replaceAll("\t", "→") || "(empty model id)";
  }

  function shortModel(model: string): string {
    const visible = visibleModel(model);
    return visible.length > 36 ? `${visible.slice(0, 36)}…` : visible;
  }

  /* A tick of several tool starts sharing a pixel column names how many, the first and last. */
  function activityTip(event: UsageActivity): { heading: string; fields: TipField[] } {
    if (event.count && event.last) {
      return {
        heading: `${formatTokens(event.count)} tool calls`,
        fields: [
          { key: "First", value: event.label, mono: true },
          { key: "Last", value: event.last.label, mono: true },
          { key: "At", value: `+${formatDuration(event.x1)} – +${formatDuration(event.last.x1)}` }
        ]
      };
    }
    return {
      heading: event.kind === "tool" ? "Tool call" : "Wait",
      fields: [
        { key: event.kind === "tool" ? "Tool" : "For", value: event.label, mono: event.kind === "tool" },
        { key: "At", value: `+${formatDuration(event.x1)}` },
        { key: "Took", value: formatElapsedDuration(event.durationSeconds * 1000) }
      ]
    };
  }

  /* Each drawn line's total after this slot, a lower bound once one of its calls went
     silent. The card's body rather than its fields: a key is an uppercased label that
     never wraps, and a model id is neither. */
  function lineTotals(slot: UsageSlot): string | null {
    if (layout.lines.length < 2) return null;
    const at = layout.slots.findIndex((candidate) => candidate.index === slot.index);
    return layout.lines
      .filter((line) => line.from <= at)
      .map((line) => `${shortModel(line.label)}: ${line.unknownFrom >= 0 && line.unknownFrom <= at ? "≥ " : ""}${formatTokens(line.running[at] ?? 0)}`)
      .join("\n");
  }

  /* The chart is an image to assistive tech, so the numbers it encodes are stated here. */
  const summary = $derived(
    `${calls} model ${calls === 1 ? "call" : "calls"} over ${formatDuration(layout.span)}, ` +
      `${unknownCalls > 0 ? "at least " : ""}${formatTokens(finalTotal)} tokens in all` +
      (unknownCalls > 0 ? `; ${unknownCalls} reported no usage` : "") +
      (categories.length > 0
        ? `. Each bar stacks ${categories.map((category) => TOKEN_CATEGORY_STYLES[category].label).join(", ")}, bottom to top; at the cursor ` +
          categories.map((category) => `${TOKEN_CATEGORY_STYLES[category].label} ${formatTokens(cursorParts[category])}`).join(", ")
        : "") +
      `. On log scales, ${showTotal ? "a heavier line shows the run's total and " : ""}lines show the running total of ${run.lines.map((line) => shortModel(line.label)).join(", ")}` +
      (waited >= 1 ? `; approval and callback waits took ${formatDuration(waited)}` : "") +
      `. Replay is at +${formatDuration(cursor?.x ?? 0)}.`
  );
</script>

<section class="usage-chart" aria-label="Tokens over time">
  {#snippet entry(key: SeriesKey, swatch: string, name: string, full: string, background?: string)}
    <li>
      <button
        type="button"
        class="entry"
        aria-pressed={!hidden.has(key)}
        aria-label={full === name ? undefined : full}
        data-tip={full === name ? undefined : full}
        onclick={() => toggle(key)}
      ><span class="swatch {swatch}" style:background={background}></span><span class="line-name" dir="auto">{name}</span></button>
    </li>
  {/snippet}

  <!-- One wrapping row of switches, so none is clipped out of reach. Left to right
       in any page because the plot's axes do not mirror. Time and the log grid are
       the axes; this row only names the series, and it is absent when there is
       nothing to name so the plot starts at the top. -->
  {#if calls > 0 || hasTools || hasWaits}
    <ul class="legend" dir="ltr" aria-label="Series shown">
      {#if calls > 0}
        {@const label = grouped ? "Calls by time" : "Each call"}
        {@render entry("calls", "bar", label, label, callsSwatch)}
      {/if}
      {#if unknownCalls > 0}<li class="key"><span class="swatch unknown"></span>No usage</li>{/if}
      {#if showTotal}{@render entry("total", "line total", "Total", "Total", TOTAL_LINE_STYLE.color)}{/if}
      {#each run.lines as line (line.label)}
        {@const name = line.line == null ? line.label : shortModel(line.label)}
        {@render entry(modelKey(line), "line", name, line.label, lineSwatch(lineStyle(line.line)))}
      {/each}
      {#if hasTools}{@render entry("tools", "tool", "Tools", "Tools")}{/if}
      {#if hasWaits}
        {@const label = `Approval / callback waits${waited >= 1 ? ` · ${formatDuration(waited)}` : ""}`}
        {@render entry("waits", "wait", label, label)}
      {/if}
    </ul>
  {/if}

  <div class="plot" style={`--plot-min: ${PLOT_MIN}px; --plot-max: ${PLOT_MAX}px`} bind:clientHeight={plotBox} bind:clientWidth={plotWidth}>
    {#if allHidden}<p class="all-hidden">Every series is hidden. Pick one in the legend to show it.</p>{/if}
    <Chart {definition} height={plotHeight} class="usage-plot" ariaLabel="Tokens per model call and running total over run time" ariaDescription={summary}>
      {#snippet tooltipBody({ points: hovered })}
        {@const event = hoveredActivity(hovered)}
        {@const slot = hoveredSlot(hovered)}
        {#if event}
          <TipCard {...activityTip(event)} />
        {:else if slot}
          <TipCard
            heading={slot.tokens > 0
              ? `${formatTokens(slot.tokens)} tok${slot.unknown > 0 ? ` + ${slot.unknown} unreported` : ""}`
              : "No usage reported"}
            fields={slotTipFields(slot, categories)}
            body={lineTotals(slot)}
          />
        {/if}
      {/snippet}
    </Chart>
  </div>

  <!-- Each figure behind the tooltip's icon for it, worded the same way, so the two read as one. -->
  {#snippet reading(metric: UsageMetric, text: string)}
    {@const Icon = metricIcon(metric)}
    <span class="reading"><Icon size={12} />{text}</span>
  {/snippet}

  {#if cursorCall}
    {@const seconds = cursorCall.durationSeconds}
    {@const ModelIcon = metricIcon("model")}
    <p class="cursor-call" dir="ltr">
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <span class="reading cursor-model" tabindex="0" data-tip={cursorCall.model} data-tip-align="start">
        <ModelIcon size={12} /><span class="fade-end" dir="auto">{visibleModel(cursorCall.model)}</span>
      </span>
      {@render reading("at", `+${formatDuration(Math.max(0, cursorCall.timestamp - origin))}`)}
      {@render reading("call", `call ${formatTokens(cursorOrdinal)}`)}
      {#if cursorCall.call}
        {@render reading("input", `${compactTokens.format(cursorCall.call.input)} in`)}
        {@render reading("output", `${compactTokens.format(cursorCall.call.output)} out`)}
        {@render reading("cached", `${compactTokens.format(cursorCall.call.cached)} cache`)}
      {:else}
        <span>Usage not reported</span>
      {/if}
      {#if seconds != null}{@render reading("duration", formatElapsedDuration(seconds * 1000))}{/if}
      <!-- A call that took no measurable time has no rate; dividing by a floor invents one. -->
      {#if cursorCall.call && seconds != null && seconds > 0 && Number.isFinite(cursorCall.call.total)}
        {@render reading("rate", `${compactRate.format(Math.round(cursorCall.call.total / seconds))} tok/s`)}
      {/if}
    </p>
  {/if}

  <p class="visually-hidden">{summary}</p>
</section>

<style>
  .usage-chart {
    /* Behind the model lines and lighter than them: the calls are the detail, the
       totals the reading. Mixed with the pane's surface rather than made translucent,
       so a bar is one color whatever is behind it. */
    --usage-bar: color-mix(in srgb, var(--model) 45%, var(--surface-1));
    --usage-unknown: color-mix(in srgb, var(--text-3) 32%, transparent);
    --usage-marker: color-mix(in srgb, var(--text-1) 55%, transparent);
    --usage-tool: color-mix(in srgb, var(--tool) 65%, transparent);
    --usage-wait: color-mix(in srgb, var(--live) 65%, transparent);
    --usage-wait-fill: color-mix(in srgb, var(--live) 18%, transparent);

    /* As tall as its legend and plot, never the pane: what a tall pane has left
       goes to the model list under it. */
    flex: none;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gutter-tight);
  }

  /* Wraps onto more rows rather than clipping an entry and the switch with it. */
  .legend {
    flex: none;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    column-gap: 10px;
    row-gap: 2px;
    margin: 0;
    padding: 0;
    list-style: none;
    color: var(--text-2);
    font-size: var(--font-xs);
  }

  .legend li {
    min-width: 0;
    max-width: 100%;
    display: inline-flex;
  }

  .key,
  .entry {
    min-width: 0;
    max-width: 100%;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    white-space: nowrap;
  }

  .entry {
    margin: 0;
    padding: 0;
    border: 0;
    background: none;
    color: inherit;
    font: inherit;
    cursor: pointer;
  }

  .entry:hover {
    color: var(--text-1);
  }

  /* Off: the name struck through and dimmed, its sample faded, in the same box. */
  .entry[aria-pressed="false"] {
    color: var(--text-3);
  }

  .entry[aria-pressed="false"] .line-name {
    text-decoration: line-through;
  }

  .entry[aria-pressed="false"] .swatch {
    opacity: 0.35;
  }

  .swatch {
    flex: none;
    display: inline-block;
    width: 10px;
    height: 10px;
  }

  .line-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .swatch.bar { background: var(--usage-bar); }
  .swatch.line { width: 16px; height: 2px; }
  .swatch.line.total { height: 3px; }
  .swatch.unknown { background: var(--usage-unknown); }
  .swatch.tool { width: 2px; background: var(--usage-tool); }
  .swatch.wait { height: 4px; box-sizing: border-box; border: 1px solid var(--usage-wait); background: var(--usage-wait-fill); }

  /* About 2.5:1 on a mid-width pane. `cqi` is the reading's own width
     (UsageReading is an inline-size container). */
  .plot {
    position: relative;
    flex: none;
    min-width: 0;
    height: clamp(var(--plot-min), 40cqi, var(--plot-max));
  }

  .all-hidden {
    position: absolute;
    inset: 0;
    z-index: 1;
    display: grid;
    place-items: center;
    margin: 0;
    padding: var(--gutter);
    color: var(--text-3);
    font-size: var(--font-xs);
    text-align: center;
    pointer-events: none;
  }

  /* The adapter renders its own SVG, so the token layer reaches it through
     descendant selectors rather than through props. */
  /* Out of flow, so the SVG's own height never props the box open: a pane that
     shrinks shrinks the box, and the measured height follows it down. */
  .plot :global(.usage-plot) {
    position: absolute !important;
    inset: 0;
    overflow: visible;
  }

  .plot :global(.usage-plot text) {
    fill: var(--text-3);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
  }

  .plot :global(.usage-plot .ts-chart__grid line),
  .plot :global(.usage-plot .ts-chart__axes line),
  .plot :global(.usage-plot .ts-chart__axes line),
  .plot :global(.usage-plot .ts-chart__axes path) {
    stroke: var(--border);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  .plot :global(.tip-card) {
    max-width: 280px;
  }

  .cursor-call {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 2px var(--gutter);
    margin: 0;
    color: var(--text-2);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  /* The icons part the figures, so a wrapped line never starts with a separator. */
  .reading {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .reading > :global(svg) {
    flex-shrink: 0;
    color: var(--text-3);
  }

  .cursor-model {
    min-width: 6ch;
    max-width: 24ch;
    color: var(--text-1);
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    border: 0;
  }

  @media (prefers-reduced-motion: reduce) {
    .plot :global(.usage-plot *) {
      transition: none !important;
      animation: none !important;
    }
  }
</style>
