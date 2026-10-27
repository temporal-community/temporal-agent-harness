<!-- One line per series over evenly spaced x values, each named at its end when there are a few.
     Hovering anywhere shows a crosshair at the nearest x value and every series' value there. -->
<script lang="ts">
  import { formatNumber, niceTicks } from "./analysis.js";
  import { seriesColor, type Series, type Tip } from "./Chart.svelte";

  interface Props {
    width: number;
    xValues: string[];
    series: Series[];
    onhover: (tip: Tip | null) => void;
  }
  let { width, xValues, series, onhover }: Props = $props();

  const H = 250;
  const labelled = $derived(series.length <= 4);
  const M = $derived({ top: 12, right: labelled ? 92 : 12, bottom: 26, left: 46 });

  const values = $derived(series.flatMap((s) => s.values).filter(Number.isFinite));
  const ticks = $derived(niceTicks(Math.min(...values), Math.max(...values)));
  const lo = $derived(ticks[0] ?? 0);
  const hi = $derived(ticks.at(-1) ?? 1);
  const plotW = $derived(width - M.left - M.right);
  const plotH = $derived(H - M.top - M.bottom);
  const x = (i: number) => M.left + (xValues.length > 1 ? (plotW * i) / (xValues.length - 1) : plotW / 2);
  const y = (v: number) => M.top + plotH * (1 - (v - lo) / (hi - lo || 1));
  // Every k-th x label, so they never overlap.
  const every = $derived(Math.max(1, Math.ceil((xValues.length * 64) / Math.max(1, plotW))));

  const paths = $derived(
    series.map((s) => s.values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(""))
  );
  // End labels, nudged apart so they don't overlap.
  const ends = $derived.by(() => {
    const placed = series
      .map((s, n) => ({ n, name: s.name, y: y(s.values.at(-1) ?? 0) }))
      .sort((a, b) => a.y - b.y);
    for (let i = 1; i < placed.length; i++) {
      const prev = placed[i - 1]!;
      if (placed[i]!.y - prev.y < 13) placed[i]!.y = prev.y + 13;
    }
    return placed;
  });

  let hovered = $state<number | null>(null);

  function move(e: PointerEvent): void {
    const rect = (e.currentTarget as SVGRectElement).getBoundingClientRect();
    const fraction = (e.clientX - rect.left) / Math.max(1, rect.width);
    const i = Math.max(0, Math.min(xValues.length - 1, Math.round(fraction * (xValues.length - 1))));
    if (i === hovered) return;
    hovered = i;
    onhover({
      x: x(i),
      y: M.top + plotH / 2,
      title: xValues[i] ?? "",
      rows: series.map((s, n) => ({ color: seriesColor(n), label: s.name, value: formatNumber(s.values[i] ?? NaN) }))
    });
  }

  function leave(): void {
    hovered = null;
    onhover(null);
  }
</script>

<svg {width} height={H} viewBox="0 0 {width} {H}">
  {#each ticks as t (t)}
    <line class="grid" x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)} />
    <text class="tick" x={M.left - 8} y={y(t)} dy="0.32em" text-anchor="end">{formatNumber(t)}</text>
  {/each}
  {#each xValues as label, i (i)}
    {#if i % every === 0 || i === xValues.length - 1}
      <text class="xl" x={x(i)} y={H - 8} text-anchor={i === 0 ? "start" : i === xValues.length - 1 ? "end" : "middle"}>{label}</text>
    {/if}
  {/each}
  {#if hovered !== null}
    <line class="cross" x1={x(hovered)} x2={x(hovered)} y1={M.top} y2={M.top + plotH} />
  {/if}
  {#each paths as d, n (n)}
    <path {d} stroke={seriesColor(n)} />
  {/each}
  {#if hovered !== null}
    {#each series as s, n (n)}
      {@const v = s.values[hovered]}
      {#if v !== undefined && Number.isFinite(v)}
        <circle class="dot" cx={x(hovered)} cy={y(v)} r="4.5" fill={seriesColor(n)} />
      {/if}
    {/each}
  {/if}
  {#if labelled}
    {#each ends as e (e.n)}
      <text class="end" x={M.left + plotW + 8} y={e.y} dy="0.32em">{e.name.length > 13 ? `${e.name.slice(0, 12)}…` : e.name}</text>
    {/each}
  {/if}
  <rect class="hit" role="presentation" x={M.left} y={M.top} width={Math.max(0, plotW)} height={plotH} onpointermove={move} onpointerleave={leave} />
</svg>

<style>
  svg { display: block; overflow: visible; }
  .grid { stroke: var(--line); }
  .tick, .xl { font-size: 10.5px; fill: var(--faint); font-family: var(--mono); }
  .end { font-size: 11px; fill: var(--muted); }
  path { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .cross { stroke: var(--line-2); stroke-dasharray: 3 3; }
  .dot { stroke: var(--panel-2); stroke-width: 2; }
  .hit { fill: transparent; }
</style>
