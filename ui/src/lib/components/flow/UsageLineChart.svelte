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

  /* The renderer sizes its own SVG from this number and writes the result inline,
     which outranks any height the stylesheet asks for. Inside the popover this
     never showed, because that card was only ever about as tall as the fixed
     figure. A pane is resizable, so the card stretches and a fixed plot would sit
     in the top 132px of a 500px box under a lake of empty. Measure the box and
     hand the renderer the height it actually has; the fixed figure stays as the
     floor for the stacked form, where the card is auto-height and takes its size
     from the plot rather than the other way round. */
  let plotBox = $state(0);
  const plotHeight = $derived(Math.max(PLOT_HEIGHT, Math.round(plotBox)));

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
     edge, and the all-zero floor of 4 keeps its two ticks on whole tokens. */
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
                /* Axis ticks land on whole steps, so the shared formatter's trailing
                   zero fields are dead weight there: "1h 00m 00s" is eleven characters
                   on a 240px axis, wide enough that the thinner drops a neighbour to
                   fit it. The header keeps the full form, where the seconds matter. */
                format: (seconds: number) =>
                  formatDuration(seconds).replace(/( 00m)? 00s$/, "")
              },
              tickLabels: { thin: { minGap: 12, priority: "ends" } }
            }
          },
          y: {
            scale: scaleLinear().domain(yDomain),
            /* Not niced, for the reason the x axis is not: nicing rounds the TOP of
               the domain out to a whole tick, so a run peaking at 430k grew a
               600,000 axis and drew the series in the bottom 70% of the box. The
               domain is already floored and has its own headroom; the ticks below
               land on round tokens inside it instead. */
            nice: false,
            grid: true,
            axis: {
              ticks: {
                values: niceTokenTicks(peakTokens),
                format: (value: number) => formatTokens(value)
              },
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

  /* Round token counts from 0 up to the peak, on the 1/2/5 steps a reader
     recognises — the linear twin of niceTimeTicks, and the reason the y domain
     can stop asking the library to nice it. Every tick sits AT OR BELOW the
     peak, so the axis never grows to reach its own last label. */
  function niceTokenTicks(peak: number, targetCount = 3): number[] {
    /* A run that billed nothing has no peak to step towards; the ends of the
       all-zero domain floor above are the only two labels available. */
    if (!Number.isFinite(peak) || peak <= 0) return [0, 4];
    const rough = peak / Math.max(2, targetCount);
    const magnitude = 10 ** Math.floor(Math.log10(rough));
    const step =
      [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((candidate) => candidate >= rough) ??
      magnitude * 10;

    const ticks: number[] = [];
    /* Re-derived from the index rather than accumulated, so float drift cannot
       shift a late tick off a round number. */
    while (ticks.length * step <= peak + 1e-9 && ticks.length <= 64) {
      ticks.push(Number((ticks.length * step).toPrecision(12)));
    }
    return ticks.length >= 2 ? ticks : [0, peak];
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
    <div
      class="plot"
      style={`--plot-height: ${PLOT_HEIGHT}px`}
      bind:clientHeight={null, (value) => (plotBox = value ?? 0)}
    >
      <Chart
        {definition}
        height={plotHeight}
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
    /* Same inset as the cards beside and below it, so the three headings share
       one baseline. */
    padding: var(--gutter);
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

  /* Grows into whatever height the card is stretched to, with the fixed height as
     a floor. The renderer does not measure this box, so the script reads it and
     passes the height down; these two rules only make the box itself stretch. */
  .plot {
    position: relative;
    min-width: 0;
    height: 100%;
    min-height: var(--plot-height);
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
