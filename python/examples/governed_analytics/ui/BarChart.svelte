<!-- Grouped bars: one group per category, one bar per series, rounded at the data end. Hovering a
     category's band shows each series' value. -->
<script module lang="ts">
  /** A bar from `base` to `end` (either side of it), its corners at `end` rounded. */
  export function barPath(x: number, w: number, base: number, end: number): string {
    const r = Math.min(4, w / 2, Math.abs(base - end));
    const dir = end < base ? 1 : -1;
    return `M${x},${base}V${end + dir * r}Q${x},${end} ${x + r},${end}H${x + w - r}Q${x + w},${end} ${x + w},${end + dir * r}V${base}Z`;
  }
</script>

<script lang="ts">
  import { formatNumber, niceTicks } from "./analysis.js";
  import { seriesColor, type Series, type Tip } from "./Chart.svelte";

  interface Props {
    width: number;
    categories: string[];
    series: Series[];
    onhover: (tip: Tip | null) => void;
  }
  let { width, categories, series, onhover }: Props = $props();

  const H = 250;
  const M = { top: 10, right: 8, bottom: 0, left: 46 };

  const values = $derived(series.flatMap((s) => s.values));
  const ticks = $derived(niceTicks(Math.min(0, ...values), Math.max(0, ...values)));
  const lo = $derived(ticks[0] ?? 0);
  const hi = $derived(ticks.at(-1) ?? 1);
  const band = $derived((width - M.left - M.right) / Math.max(1, categories.length));
  // Long or many category names lean over rather than collide.
  const slanted = $derived(categories.some((c) => c.length * 6.4 > band - 6));
  const bottom = $derived(slanted ? Math.min(90, 16 + Math.max(...categories.map((c) => Math.min(c.length, 18))) * 4.6) : 26);
  const plotH = $derived(H - M.top - bottom);
  const y = (v: number) => M.top + plotH * (1 - (v - lo) / (hi - lo || 1));
  const inner = $derived(Math.min(band * 0.78, series.length * 28));
  const barW = $derived(Math.max(2, (inner - (series.length - 1) * 2) / Math.max(1, series.length)));
  const x0 = (i: number) => M.left + i * band + (band - inner) / 2;
  const short = (s: string) => (s.length > 18 ? `${s.slice(0, 17)}…` : s);

  let hovered = $state<number | null>(null);

  function hover(i: number | null): void {
    hovered = i;
    if (i === null) return onhover(null);
    const top = Math.min(...series.map((s) => y(Math.max(0, s.values[i] ?? 0))));
    onhover({
      x: M.left + (i + 0.5) * band,
      y: Math.max(30, top),
      title: categories[i] ?? "",
      rows: series.map((s, n) => ({ color: seriesColor(n), label: s.name, value: formatNumber(s.values[i] ?? NaN) }))
    });
  }
</script>

<svg {width} height={H} viewBox="0 0 {width} {H}">
  {#each ticks as t (t)}
    <line class="grid" class:zero={t === 0} x1={M.left} x2={width - M.right} y1={y(t)} y2={y(t)} />
    <text class="tick" x={M.left - 8} y={y(t)} dy="0.32em" text-anchor="end">{formatNumber(t)}</text>
  {/each}
  {#each categories as c, i (i)}
    {#if hovered === i}<rect class="band" x={M.left + i * band} y={M.top} width={band} height={plotH} rx="4" />{/if}
    {#each series as s, n (n)}
      {@const v = s.values[i] ?? 0}
      <path d={barPath(x0(i) + n * (barW + 2), barW, y(0), y(v))} fill={seriesColor(n)} />
    {/each}
    {#if slanted}
      <text class="cat" transform="translate({M.left + (i + 0.5) * band},{M.top + plotH + 12}) rotate(-35)" text-anchor="end">{short(c)}</text>
    {:else}
      <text class="cat" x={M.left + (i + 0.5) * band} y={M.top + plotH + 17} text-anchor="middle">{c}</text>
    {/if}
    <rect
      class="hit"
      role="presentation"
      x={M.left + i * band}
      y={M.top}
      width={band}
      height={plotH}
      onpointerenter={() => hover(i)}
      onpointerleave={() => hover(null)}
    />
  {/each}
</svg>

<style>
  svg { display: block; overflow: visible; }
  .grid { stroke: var(--line); stroke-width: 1; }
  .grid.zero { stroke: var(--line-2); }
  .tick, .cat { font-size: 10.5px; fill: var(--faint); font-family: var(--mono); }
  .cat { fill: var(--muted); font-family: var(--sans); }
  .band { fill: var(--panel-3); }
  .hit { fill: transparent; }
</style>
