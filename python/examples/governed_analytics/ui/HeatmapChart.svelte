<!-- A grid of cells shaded by value, like a correlation's heat table: blue above zero, orange below,
     the panel near zero, scaled to the largest magnitude. Hovering a cell shows its row, column and
     value. -->
<script lang="ts">
  import { formatNumber } from "./analysis.js";
  import { seriesColor, type Tip } from "./Chart.svelte";

  interface Props {
    width: number;
    rowLabels: string[];
    columnLabels: string[];
    values: number[][];
    valueLabel: string;
    onhover: (tip: Tip | null) => void;
  }
  let { width, rowLabels, columnLabels, values, valueLabel, onhover }: Props = $props();

  const short = (s: string) => (s.length > 18 ? `${s.slice(0, 17)}…` : s);
  const longest = (labels: string[]) => Math.max(0, ...labels.map((l) => short(l).length));

  const left = $derived(Math.min(140, 12 + longest(rowLabels) * 6.6));
  const cellW = $derived((width - left - 4) / Math.max(1, columnLabels.length));
  // Long or many column names lean over rather than collide.
  const slanted = $derived(columnLabels.some((c) => short(c).length * 6.4 > cellW - 6));
  const top = $derived(slanted ? Math.min(100, 14 + longest(columnLabels) * 4.6) : 22);
  const cellH = $derived(Math.max(18, Math.min(38, cellW * 0.6)));
  const H = $derived(top + cellH * rowLabels.length + 2);
  const scale = $derived(Math.max(...values.flat().map(Math.abs), 0) || 1);
  const showValues = $derived(cellW >= 46 && cellH >= 20);

  function fill(v: number): string {
    const hue = v >= 0 ? seriesColor(0) : seriesColor(1);
    return `color-mix(in srgb, ${hue} ${Math.round((Math.abs(v) / scale) * 70)}%, var(--panel-2))`;
  }

  let hovered = $state<[number, number] | null>(null);

  function hover(r: number, c: number): void {
    hovered = [r, c];
    const v = values[r]?.[c] ?? NaN;
    onhover({
      x: left + (c + 0.5) * cellW,
      y: top + (r + 0.5) * cellH,
      title: `${rowLabels[r] ?? ""} × ${columnLabels[c] ?? ""}`,
      rows: [{ color: fill(v), label: valueLabel || "value", value: formatNumber(v) }]
    });
  }

  function leave(): void {
    hovered = null;
    onhover(null);
  }
</script>

<svg {width} height={H} viewBox="0 0 {width} {H}" role="presentation" onpointerleave={leave}>
  {#each columnLabels as c, i (i)}
    {#if slanted}
      <text class="label" transform="translate({left + (i + 0.5) * cellW},{top - 6}) rotate(-35)">{short(c)}</text>
    {:else}
      <text class="label" x={left + (i + 0.5) * cellW} y={top - 8} text-anchor="middle">{short(c)}</text>
    {/if}
  {/each}
  {#each rowLabels as rowLabel, r (r)}
    <text class="label" x={left - 8} y={top + (r + 0.5) * cellH} dy="0.32em" text-anchor="end">{short(rowLabel)}</text>
    {#each columnLabels as _, c (c)}
      {@const v = values[r]?.[c] ?? 0}
      <rect
        role="presentation"
        x={left + c * cellW + 1}
        y={top + r * cellH + 1}
        width={Math.max(1, cellW - 2)}
        height={cellH - 2}
        rx="3"
        fill={fill(v)}
        class:on={hovered?.[0] === r && hovered?.[1] === c}
        onpointerenter={() => hover(r, c)}
      />
      {#if showValues}
        <text class="value" x={left + (c + 0.5) * cellW} y={top + (r + 0.5) * cellH} dy="0.32em" text-anchor="middle">{formatNumber(v)}</text>
      {/if}
    {/each}
  {/each}
</svg>

<style>
  svg { display: block; overflow: visible; }
  .label { font-size: 10.5px; fill: var(--muted); font-family: var(--mono); }
  .value { font-size: 10.5px; fill: var(--ink); font-family: var(--mono); pointer-events: none; }
  rect { stroke: transparent; stroke-width: 1.5; }
  rect.on { stroke: var(--line-2); }
</style>
