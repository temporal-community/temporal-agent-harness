<!-- Slices of a whole, drawn as a ring with the total in its middle and each slice's share beside
     it. Past eight slices, the smallest fold into "Other". -->
<script lang="ts">
  import { formatNumber } from "./analysis.js";
  import { seriesColor, type Tip } from "./Chart.svelte";

  interface Props {
    width: number;
    labels: string[];
    values: number[];
    onhover: (tip: Tip | null) => void;
  }
  let { width, labels, values, onhover }: Props = $props();

  const slices = $derived.by(() => {
    const all = labels.map((label, i) => ({ label, value: Math.max(0, values[i] ?? 0) }));
    if (all.length <= 8) return all;
    const sorted = [...all].sort((a, b) => b.value - a.value);
    const rest = sorted.slice(7).reduce((sum, s) => sum + s.value, 0);
    return [...sorted.slice(0, 7), { label: `Other (${sorted.length - 7})`, value: rest }];
  });
  const total = $derived(slices.reduce((sum, s) => sum + s.value, 0));

  const size = $derived(Math.max(150, Math.min(220, width * 0.42)));
  const R = $derived(size / 2 - 4);
  const r = $derived(R * 0.6);
  const cx = $derived(size / 2);
  const cy = $derived(size / 2);

  const arcs = $derived.by(() => {
    let angle = -Math.PI / 2;
    return slices.map((s) => {
      const sweep = total ? (s.value / total) * Math.PI * 2 : 0;
      const start = angle;
      angle += sweep;
      return { ...s, start, end: angle, share: total ? s.value / total : 0 };
    });
  });

  function arcPath(a0: number, a1: number): string {
    // A whole ring can't be one arc: draw it as two halves.
    if (a1 - a0 >= Math.PI * 2 - 1e-6) return arcPath(a0, a0 + Math.PI) + arcPath(a0 + Math.PI, a1);
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const p = (rad: number, a: number) => `${cx + rad * Math.cos(a)},${cy + rad * Math.sin(a)}`;
    return `M${p(R, a0)}A${R},${R} 0 ${large} 1 ${p(R, a1)}L${p(r, a1)}A${r},${r} 0 ${large} 0 ${p(r, a0)}Z`;
  }

  let hovered = $state<number | null>(null);

  function hover(i: number | null): void {
    hovered = i;
    const a = i === null ? undefined : arcs[i];
    if (!a || i === null) return onhover(null);
    const mid = (a.start + a.end) / 2;
    onhover({
      x: cx + (R + 6) * Math.cos(mid),
      y: cy + (R + 6) * Math.sin(mid),
      title: a.label,
      rows: [{ color: seriesColor(i), label: `${(a.share * 100).toFixed(1)}% of total`, value: formatNumber(a.value) }]
    });
  }
</script>

<div class="pie">
  <svg width={size} height={size} viewBox="0 0 {size} {size}">
    {#each arcs as a, i (i)}
      {#if a.end > a.start}
        <path
          role="presentation"
          d={arcPath(a.start, a.end)}
          fill={seriesColor(i)}
          class:dim={hovered !== null && hovered !== i}
          onpointerenter={() => hover(i)}
          onpointerleave={() => hover(null)}
        />
      {/if}
    {/each}
    <text class="total" x={cx} y={cy - 2} text-anchor="middle">{formatNumber(total)}</text>
    <text class="caption" x={cx} y={cy + 15} text-anchor="middle">total</text>
  </svg>
  <ul>
    {#each arcs as a, i (i)}
      <li class:on={hovered === i} onpointerenter={() => hover(i)} onpointerleave={() => hover(null)}>
        <i style:background={seriesColor(i)}></i>
        <span>{a.label}</span>
        <em>{(a.share * 100).toFixed(1)}%</em>
      </li>
    {/each}
  </ul>
</div>

<style>
  .pie { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
  svg { display: block; flex: none; }
  path { stroke: var(--panel-2); stroke-width: 2; transition: opacity 0.15s; }
  path.dim { opacity: 0.35; }
  .total { font-size: 17px; font-weight: 600; fill: var(--ink); font-family: var(--mono); }
  .caption { font-size: 10.5px; fill: var(--faint); text-transform: uppercase; letter-spacing: 0.08em; }
  ul { flex: 1; min-width: 160px; margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
  li { display: grid; grid-template-columns: 10px 1fr auto; align-items: center; gap: 8px; padding: 3px 6px; border-radius: 6px; color: var(--muted); }
  li.on { background: var(--panel-3); color: var(--ink); }
  li i { width: 10px; height: 10px; border-radius: 3px; }
  li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  li em { font-style: normal; font-family: var(--mono); color: var(--ink); }
</style>
