<script lang="ts">
  /* Narrowest subpath per symbol, not the barrel: every mark has its own entry and
     that is the only lever on what the d3 dependency tree drags in. */
  import { areaY } from "@tanstack/charts/area";
  import { lineY } from "@tanstack/charts/line";
  import { ruleX } from "@tanstack/charts/rule";
  import { scaleLinear } from "@tanstack/charts/scales/linear";
  import { defineChart } from "@tanstack/charts/scene";
  import { Chart } from "@tanstack/charts/svelte";
  import { tooltip } from "@tanstack/charts/tooltip";
  import { formatTokens, type UsageTimelinePoint } from "$lib/cost/pricing";
  import { formatDuration } from "$lib/state/replayLog";
  import { niceTimeTicks } from "$lib/state/timeTicks";

  interface Props {
    points: UsageTimelinePoint[];
    viewIndex: number;
  }

  let { points, viewIndex }: Props = $props();

  /* One sample per timeline point, carrying elapsed seconds rather than absolute
     timestamps so the x axis reads as run duration. */
  interface Sample {
    index: number;
    elapsed: number;
    tokens: number;
    event: string;
  }

  const PLOT_HEIGHT = 132;

  const chartPoints = $derived(tokenWindow(points));
  const originTimestamp = $derived(
    chartPoints.length ? Math.min(...chartPoints.map((point) => point.timestamp)) : 0
  );
  const samples = $derived(
    collapseSameInstant(
      chartPoints
        .map((point) => ({
          index: point.index,
          elapsed: Math.max(0, point.timestamp - originTimestamp),
          tokens: point.tokens.total,
          event: point.event
        }))
        /* Timestamps can arrive out of order across agents; an unsorted series draws a
           path that doubles back on itself. */
        .sort((a, b) => a.elapsed - b.elapsed || a.index - b.index)
    )
  );

  const currentSample = $derived(latestSampleAtOrBefore(samples, viewIndex));
  const peakTokens = $derived(samples.reduce((max, s) => Math.max(max, s.tokens), 0));
  const lastElapsed = $derived(samples.length ? samples[samples.length - 1].elapsed : 0);

  /* Both domains are floored away from zero width. A flat or all-zero series would
     otherwise collapse the range and divide by zero when the scale interpolates.
     Token counts are zero-based on purpose: the series is cumulative, so a clipped
     baseline would overstate growth. The headroom keeps a flat series off the frame
     edge, and the all-zero floor of 4 keeps the niced ticks on whole tokens. */
  const yDomain = $derived<[number, number]>([
    0,
    peakTokens > 0 ? peakTokens * 1.08 : 4
  ]);
  const xDomain = $derived<[number, number]>([0, lastElapsed > 0 ? lastElapsed : 1]);

  const reducedMotion = prefersReducedMotion();

  const definition = $derived(
    defineChart({
      /* Responsive form: the tick budget is recomputed from the measured width, which
         is what keeps labels from colliding when the pane rail narrows. */
      chart: ({ width }) => ({
        marks: [
          areaY(samples, {
            x: "elapsed",
            y: "tokens",
            fill: "var(--usage-fill)",
            fillOpacity: 1
          }),
          lineY(samples, {
            x: "elapsed",
            y: "tokens",
            stroke: "var(--usage-line)",
            strokeWidth: 2,
            /* Point dots stay legible up to a few dozen samples; past that they merge
               into a band and the line alone reads better. A lone sample has no line,
               so its dot is the only thing that would render. */
            points: samples.length <= 40
          }),
          ...(currentSample
            ? [
                ruleX([currentSample.elapsed], {
                  stroke: "var(--usage-marker)",
                  strokeWidth: 1,
                  strokeDasharray: "3 4"
                })
              ]
            : [])
        ],
        scales: {
          /* Configured instances, not factories: a factory would let the engine infer
             the domain from the channels, which is exactly what collapses on a flat or
             all-zero series. These domains are already floored. */
          x: {
            scale: scaleLinear().domain(xDomain),
            /* Not niced: nicing rounds a SECONDS domain to a power of ten, so a 12,005s
               run grew a 20,000s axis and left the series stopping at 60% of the width
               under a tick reading "5h 33m 20s". The domain is already floored, and the
               ticks below are chosen on time steps instead. */
            nice: false,
            axis: {
              ticks: {
                /* A run with no measurable duration gets a single origin tick; any
                   more would all format to the same second and read as duplicates. */
                ...(lastElapsed > 0
                  ? { values: niceTimeTicks(lastElapsed, Math.max(4, Math.min(8, Math.round(width / 60)))) }
                  : { values: [0] }),
                format: formatDuration
              },
              tickLabels: { thin: { minGap: 12, priority: "ends" } }
            }
          },
          y: {
            scale: scaleLinear().domain(yDomain),
            nice: true,
            grid: true,
            axis: {
              ticks: { count: 3, format: (value: number) => formatTokens(value) },
              tickLabels: { thin: { minGap: 8 } }
            }
          }
        }
      }),
      /* 'auto' happily parks the tooltip over the x axis, hiding the very tick labels
         the hover is being read against. Preferring the sides and top keeps it clear of
         them, with 'bottom' still available when there is nowhere else to go. */
      tooltip: {
        ...tooltip,
        placement: ["top", "top-right", "top-left", "right", "left", "bottom"],
        offset: 12
      },
      /* The library gates its own transitions on the media query; the flag keeps the
         initial draw static too when motion is not wanted. */
      svgAnimation: reducedMotion ? false : { respectReducedMotion: true },
      keyboard: true
    })
  );

  /* Read once: a replay footer is not a place to re-run layout on a media change. */
  function prefersReducedMotion(): boolean {
    if (typeof window === "undefined" || !window.matchMedia) return false;
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function hasTokens(point: UsageTimelinePoint): boolean {
    return point.tokens.total > 0;
  }

  /* Trim the dead head of the run: everything before the first billed event, less one
     point so the first spike still rises from a baseline. */
  function tokenWindow(source: UsageTimelinePoint[]): UsageTimelinePoint[] {
    const firstTokenIndex = source.findIndex(hasTokens);
    if (firstTokenIndex === -1) return source;
    return source.slice(Math.max(0, firstTokenIndex - 1));
  }

  /* Several frames commonly land inside the same sampled instant, and a run of points
     sharing one x is ambiguous for a line: the series is cumulative, so the last of the
     run is the only total that was true at that instant. Collapsing here also keeps the
     area mark well formed, since a stacked area rejects a repeated position outright.
     Expects `source` sorted by (elapsed, index). */
  function collapseSameInstant(source: Sample[]): Sample[] {
    const collapsed: Sample[] = [];
    for (const sample of source) {
      const previous = collapsed[collapsed.length - 1];
      if (previous && previous.elapsed === sample.elapsed) collapsed[collapsed.length - 1] = sample;
      else collapsed.push(sample);
    }
    return collapsed;
  }

  function latestSampleAtOrBefore(source: Sample[], index: number): Sample | undefined {
    let found: Sample | undefined;
    for (const sample of source) {
      if (sample.index <= index) found = sample;
    }
    return found ?? source[0];
  }

  const currentLabel = $derived(
    currentSample
      ? `${formatTokens(currentSample.tokens)} · +${formatDuration(currentSample.elapsed)}`
      : "0"
  );

  /* The chart is an image to assistive tech, so the numbers it encodes have to be
     stated somewhere. This is that statement, and it also feeds the aria label. */
  const summary = $derived(
    samples.length === 0
      ? "No token usage recorded yet."
      : `Cumulative token usage across ${samples.length} sampled ${
          samples.length === 1 ? "event" : "events"
        }, peaking at ${formatTokens(peakTokens)} tokens over ${formatDuration(
          lastElapsed
        )}. Replay is at ${currentLabel}.`
  );
</script>

<section class="usage-chart" aria-label="Replay token usage timeline">
  <div class="chart-head">
    <span>Token total</span>
    <span class="current-value">{currentLabel}</span>
  </div>

  {#if samples.length === 0}
    <p class="chart-empty">Step through the stream to chart token usage.</p>
  {:else}
    <div class="plot" style={`--plot-height: ${PLOT_HEIGHT}px`}>
      <Chart
        {definition}
        height={PLOT_HEIGHT}
        class="usage-plot"
        ariaLabel="Cumulative token usage over run time"
        ariaDescription={summary}
      >
        {#snippet tooltipBody({ points: hovered })}
          <div class="usage-tip">
            {#each hovered as hoveredPoint (hoveredPoint.key)}
              {@const sample = hoveredPoint.datum as Sample}
              <strong>{formatTokens(sample.tokens)} tok</strong>
              <span>{sample.event} · +{formatDuration(sample.elapsed)}</span>
            {/each}
          </div>
        {/snippet}
      </Chart>
    </div>
  {/if}

  <p class="visually-hidden" aria-live="off">{summary}</p>
</section>

<style>
  .usage-chart {
    /* The amber identity comes off the token layer rather than a local hex. */
    --usage-line: var(--warning);
    --usage-fill: color-mix(in srgb, var(--warning) 14%, transparent);
    --usage-marker: color-mix(in srgb, var(--text-1) 55%, transparent);
    --usage-grid: var(--border);
    --usage-axis-text: var(--text-3);

    min-width: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    gap: 6px;
    align-self: start;
    padding: 8px 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    background: var(--surface-2);
  }

  .chart-head {
    min-width: 0;
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: center;
    color: var(--text-2);
    font-size: var(--font-sm);
  }

  .current-value {
    color: var(--text-3);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .plot {
    position: relative;
    min-width: 0;
    height: var(--plot-height);
  }

  /* The adapter renders its own SVG, so the token layer reaches it through
     descendant selectors rather than through props. */
  .plot :global(.usage-plot) {
    display: block;
    width: 100%;
    height: 100%;
    overflow: visible;
  }

  .plot :global(.usage-plot text) {
    fill: var(--usage-axis-text);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
  }

  /* Renderer-owned groups, named as the adapter emits them: ts-chart__grid and
     ts-chart__axes. Without these the lines keep the library's own defaults, which are
     not on the token layer. */
  .plot :global(.usage-plot .ts-chart__grid line),
  .plot :global(.usage-plot .ts-chart__axes line),
  .plot :global(.usage-plot .ts-chart__axes path) {
    stroke: var(--usage-grid);
    stroke-width: 1;
    vector-effect: non-scaling-stroke;
  }

  .chart-empty {
    margin: 0;
    align-self: center;
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .usage-tip {
    display: grid;
    gap: 1px;
    padding: 5px 8px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    background: var(--surface-3);
    color: var(--text-1);
    font-size: var(--font-sm);
    white-space: nowrap;
  }

  .usage-tip strong {
    font-variant-numeric: tabular-nums;
  }

  .usage-tip span {
    color: var(--text-3);
    font-size: var(--font-xs);
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
