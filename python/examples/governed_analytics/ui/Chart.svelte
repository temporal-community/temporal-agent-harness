<!-- One chart the agent drew: a render_*_chart call, drawn from the call's own input, so it is the
     same after a reload. A card with the title, a legend for two series or more, the plot, the
     hover tooltip the plots report into, and the data as a table. -->
<script module lang="ts">
  export interface Series {
    name: string;
    values: number[];
  }

  /** What the tooltip shows, at a point in the plot's own coordinates. */
  export interface Tip {
    x: number;
    y: number;
    title: string;
    rows: { color: string; label: string; value: string }[];
  }

  /** The colour of the n-th series (or slice), in the palette's fixed order. */
  export const seriesColor = (n: number) => `var(--series-${(n % 8) + 1})`;

  export const CHART_KINDS: Record<string, string> = {
    render_bar_chart: "Bar chart",
    render_line_chart: "Line chart",
    render_pie_chart: "Pie chart",
    render_scatter_chart: "Scatter plot"
  };
</script>

<script lang="ts">
  import { formatCell } from "./analysis.js";
  import BarChart from "./BarChart.svelte";
  import LineChart from "./LineChart.svelte";
  import PieChart from "./PieChart.svelte";
  import ScatterChart from "./ScatterChart.svelte";

  interface Props {
    tool: string;
    input: Record<string, unknown>;
    /** Why it can't be drawn, when it can't. */
    problem?: string | null;
  }
  let { tool, input, problem = null }: Props = $props();

  let tip = $state<Tip | null>(null);
  let showTable = $state(false);
  let width = $state(0);

  const title = $derived(String(input.title ?? "Chart"));
  const series = $derived((Array.isArray(input.series) ? input.series : []) as Series[]);
  const legend = $derived(
    tool === "render_pie_chart"
      ? []
      : series.length > 1
        ? series.map((s, i) => ({ name: s.name, color: seriesColor(i) }))
        : []
  );

  /** The chart's data as rows, for the table view. */
  const table = $derived.by((): { columns: string[]; rows: (string | number | null)[][] } => {
    if (tool === "render_bar_chart" || tool === "render_line_chart") {
      const keys = ((tool === "render_bar_chart" ? input.categories : input.x_values) ?? []) as string[];
      return {
        columns: [tool === "render_bar_chart" ? "Category" : "x", ...series.map((s) => s.name)],
        rows: keys.map((k, i) => [k, ...series.map((s) => s.values[i] ?? null)])
      };
    }
    if (tool === "render_pie_chart") {
      const labels = (input.labels ?? []) as string[];
      const values = (input.values ?? []) as number[];
      return { columns: ["Label", "Value"], rows: labels.map((l, i) => [l, values[i] ?? null]) };
    }
    const points = (input.points ?? []) as { x: number; y: number; label?: string | null }[];
    return {
      columns: ["Label", String(input.x_label ?? "x"), String(input.y_label ?? "y")],
      rows: points.map((p) => [p.label ?? "", p.x, p.y])
    };
  });
</script>

<figure class="chart">
  <figcaption>
    <div>
      <b>{title}</b>
      <small>{CHART_KINDS[tool] ?? "Chart"}{#if input.y_label} · {input.y_label}{/if}</small>
    </div>
    {#if !problem}
      <button class:on={showTable} onclick={() => (showTable = !showTable)} aria-pressed={showTable}>Table</button>
    {/if}
  </figcaption>

  {#if legend.length}
    <ul class="legend">
      {#each legend as l (l.name)}<li><i style:background={l.color}></i>{l.name}</li>{/each}
    </ul>
  {/if}

  {#if problem}
    <p class="problem">Couldn't draw this chart: {problem}</p>
  {:else if showTable}
    <div class="table">
      <table>
        <thead><tr>{#each table.columns as c, i (i)}<th>{c}</th>{/each}</tr></thead>
        <tbody>
          {#each table.rows as row, r (r)}
            <tr>{#each row as cell, c (c)}<td class:num={typeof cell === "number"}>{typeof cell === "number" ? formatCell(cell) : (cell ?? "—")}</td>{/each}</tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <div class="plot" bind:clientWidth={width} role="img" aria-label={title}>
      {#if tool === "render_bar_chart"}
        <BarChart width={width || 560} categories={(input.categories ?? []) as string[]} {series} onhover={(t) => (tip = t)} />
      {:else if tool === "render_line_chart"}
        <LineChart width={width || 560} xValues={(input.x_values ?? []) as string[]} {series} onhover={(t) => (tip = t)} />
      {:else if tool === "render_pie_chart"}
        <PieChart width={width || 560} labels={(input.labels ?? []) as string[]} values={(input.values ?? []) as number[]} onhover={(t) => (tip = t)} />
      {:else}
        <ScatterChart
          width={width || 560}
          points={(input.points ?? []) as { x: number; y: number; label?: string | null }[]}
          xLabel={String(input.x_label ?? "")}
          yLabel={String(input.y_label ?? "")}
          onhover={(t) => (tip = t)}
        />
      {/if}
      {#if tip}
        <div class="tip" style:left="{tip.x}px" style:top="{tip.y}px" class:flip={tip.x > (width || 560) * 0.62}>
          <b>{tip.title}</b>
          {#each tip.rows as row, i (i)}
            <div class="row"><i style:background={row.color}></i><span>{row.label}</span><em>{row.value}</em></div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</figure>

<style>
  .chart { margin: 4px 0 8px; padding: 12px 14px 10px; background: var(--panel-2); border: 1px solid var(--line); border-radius: var(--radius); animation: rise 0.3s ease-out; }
  figcaption { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
  figcaption b { display: block; font-weight: 600; font-size: 13.5px; letter-spacing: -0.01em; }
  figcaption small { color: var(--faint); font-size: 11.5px; }
  figcaption button { font-size: 11px; padding: 2px 9px; border-radius: 999px; border: 1px solid var(--line); background: transparent; color: var(--muted); }
  figcaption button.on, figcaption button:hover { color: var(--ink); border-color: var(--line-2); }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 8px 0 0; padding: 0; list-style: none; font-size: 11.5px; color: var(--muted); }
  .legend li { display: flex; align-items: center; gap: 6px; }
  .legend i, .tip i { width: 9px; height: 9px; border-radius: 3px; flex: none; }
  .plot { position: relative; margin-top: 8px; }
  .problem { margin: 8px 0 2px; color: var(--warn); font-size: 12.5px; }
  .tip {
    position: absolute;
    z-index: 2;
    transform: translate(12px, -50%);
    min-width: 140px;
    max-width: 260px;
    padding: 7px 9px;
    border-radius: 8px;
    background: var(--panel-3);
    border: 1px solid var(--line-2);
    box-shadow: var(--shadow);
    pointer-events: none;
    font-size: 11.5px;
  }
  .tip.flip { transform: translate(calc(-100% - 12px), -50%); }
  .tip b { display: block; margin-bottom: 3px; font-weight: 600; }
  .tip .row { display: flex; align-items: center; gap: 6px; color: var(--muted); }
  .tip .row span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tip em { font-style: normal; font-family: var(--mono); color: var(--ink); }
  .table { margin-top: 8px; max-height: 280px; overflow: auto; border: 1px solid var(--line); border-radius: 7px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { padding: 4px 10px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { position: sticky; top: 0; background: var(--panel-3); font-weight: 600; color: var(--muted); }
  td.num { text-align: right; font-family: var(--mono); }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); } }
</style>
