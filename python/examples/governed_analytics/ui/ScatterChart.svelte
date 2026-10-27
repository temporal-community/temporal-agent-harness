<!-- Points against two axes. Hovering near a point shows its label and both values. -->
<script lang="ts">
  import { formatNumber, niceTicks } from "./analysis.js";
  import { seriesColor, type Tip } from "./Chart.svelte";

  interface Point {
    x: number;
    y: number;
    label?: string | null;
  }
  interface Props {
    width: number;
    points: Point[];
    xLabel: string;
    yLabel: string;
    onhover: (tip: Tip | null) => void;
  }
  let { width, points, xLabel, yLabel, onhover }: Props = $props();

  const H = 270;
  const M = { top: 10, right: 14, bottom: 40, left: 56 };

  const xTicks = $derived(niceTicks(Math.min(...points.map((p) => p.x)), Math.max(...points.map((p) => p.x))));
  const yTicks = $derived(niceTicks(Math.min(...points.map((p) => p.y)), Math.max(...points.map((p) => p.y))));
  const plotW = $derived(width - M.left - M.right);
  const plotH = H - M.top - M.bottom;
  const scale = (v: number, ticks: number[], from: number, span: number) => {
    const lo = ticks[0] ?? 0;
    const hi = ticks.at(-1) ?? 1;
    return from + (span * (v - lo)) / (hi - lo || 1);
  };
  const sx = (v: number) => scale(v, xTicks, M.left, plotW);
  const sy = (v: number) => scale(v, yTicks, M.top + plotH, -plotH);

  let hovered = $state<number | null>(null);

  function move(e: PointerEvent): void {
    const rect = (e.currentTarget as SVGRectElement).getBoundingClientRect();
    const px = M.left + ((e.clientX - rect.left) / Math.max(1, rect.width)) * plotW;
    const py = M.top + ((e.clientY - rect.top) / Math.max(1, rect.height)) * plotH;
    let best: number | null = null;
    let bestD = 18 * 18;
    for (const [i, p] of points.entries()) {
      const d = (sx(p.x) - px) ** 2 + (sy(p.y) - py) ** 2;
      if (d < bestD) [best, bestD] = [i, d];
    }
    if (best === hovered) return;
    hovered = best;
    const p = best === null ? undefined : points[best];
    if (!p) return onhover(null);
    onhover({
      x: sx(p.x),
      y: sy(p.y),
      title: p.label || `Point ${(best ?? 0) + 1}`,
      rows: [
        { color: seriesColor(0), label: xLabel || "x", value: formatNumber(p.x) },
        { color: seriesColor(0), label: yLabel || "y", value: formatNumber(p.y) }
      ]
    });
  }

  function leave(): void {
    hovered = null;
    onhover(null);
  }
</script>

<svg {width} height={H} viewBox="0 0 {width} {H}">
  {#each yTicks as t (t)}
    <line class="grid" x1={M.left} x2={M.left + plotW} y1={sy(t)} y2={sy(t)} />
    <text class="tick" x={M.left - 8} y={sy(t)} dy="0.32em" text-anchor="end">{formatNumber(t)}</text>
  {/each}
  {#each xTicks as t (t)}
    <line class="grid" x1={sx(t)} x2={sx(t)} y1={M.top} y2={M.top + plotH} />
    <text class="tick" x={sx(t)} y={M.top + plotH + 15} text-anchor="middle">{formatNumber(t)}</text>
  {/each}
  <text class="axis" x={M.left + plotW / 2} y={H - 4} text-anchor="middle">{xLabel}</text>
  <text class="axis" transform="translate(12,{M.top + plotH / 2}) rotate(-90)" text-anchor="middle">{yLabel}</text>
  {#each points as p, i (i)}
    <circle cx={sx(p.x)} cy={sy(p.y)} r={hovered === i ? 6 : 4.5} fill={seriesColor(0)} class:on={hovered === i} />
  {/each}
  <rect class="hit" role="presentation" x={M.left} y={M.top} width={Math.max(0, plotW)} height={plotH} onpointermove={move} onpointerleave={leave} />
</svg>

<style>
  svg { display: block; overflow: visible; }
  .grid { stroke: var(--line); }
  .tick { font-size: 10.5px; fill: var(--faint); font-family: var(--mono); }
  .axis { font-size: 11px; fill: var(--muted); }
  circle { stroke: var(--panel-2); stroke-width: 2; fill-opacity: 0.85; }
  circle.on { fill-opacity: 1; }
  .hit { fill: transparent; }
</style>
