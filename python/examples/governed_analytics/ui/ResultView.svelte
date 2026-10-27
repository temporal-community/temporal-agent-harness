<!-- What a data tool returned, drawn for its shape: a query's rows as a table, summary statistics
     with a box plot, a distribution as a histogram, a correlation matrix as a heat table, a group
     comparison, outliers, and the catalog lookups. Anything else shows as the raw text. -->
<script lang="ts">
  import { formatCell, formatNumber, type PyValue } from "./analysis.js";

  interface Props {
    tool: string;
    result: PyValue | undefined;
    raw: string;
  }
  let { tool, result, raw }: Props = $props();

  type Row = unknown[];
  const fields = (v: unknown) => (v && typeof v === "object" && !Array.isArray(v) ? v : {}) as Record<string, unknown>;
  const num = (v: unknown) => (typeof v === "number" ? v : null);
  const list = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
  const obj = $derived(fields(result));

  const STAT_KEYS = ["count", "nulls", "mean", "std", "min", "p25", "median", "p75", "max"] as const;

  // Summary statistics' box plot.
  const box = $derived.by(() => {
    const [min, p25, median, p75, max] = (["min", "p25", "median", "p75", "max"] as const).map((k) => num(obj[k]));
    if ([min, p25, median, p75, max].some((v) => v === null)) return null;
    const span = max! - min! || 1;
    const at = (v: number) => 8 + ((v - min!) / span) * 284;
    return { min: at(min!), p25: at(p25!), median: at(median!), p75: at(p75!), max: at(max!) };
  });

  // A distribution's histogram.
  const bins = $derived(list(obj.bins).map((b) => fields(b)).map((b) => ({ lower: num(b.lower) ?? 0, upper: num(b.upper) ?? 0, count: num(b.count) ?? 0 })));
  const binMax = $derived(Math.max(1, ...bins.map((b) => b.count)));

  // A correlation's heat cells: blue for positive, orange for negative, grey near zero.
  function heat(r: number | null): string {
    if (r === null) return "transparent";
    const hue = r >= 0 ? "var(--series-1)" : "var(--series-2)";
    return `color-mix(in srgb, ${hue} ${Math.round(Math.abs(r) * 70)}%, var(--panel-2))`;
  }
</script>

{#snippet table(columns: string[], rows: Row[], truncated = false)}
  <div class="table">
    <table>
      <thead><tr>{#each columns as c, i (i)}<th>{c}</th>{/each}</tr></thead>
      <tbody>
        {#each rows as row, r (r)}
          <tr>{#each row as cell, c (c)}<td class:num={typeof cell === "number"}>{formatCell(cell)}</td>{/each}</tr>
        {:else}
          <tr><td class="empty" colspan={Math.max(1, columns.length)}>No rows.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="meta">{rows.length} row{rows.length === 1 ? "" : "s"}{truncated ? " shown · the query returned more" : ""}</p>
{/snippet}

{#if result === undefined}
  <pre class="raw">{raw}</pre>
{:else if tool === "run_sql"}
  {@render table(list(obj.columns).map(String), list(obj.rows) as Row[], obj.truncated === true)}
{:else if tool === "detect_outliers"}
  <p class="stats-line">
    <b>{String(obj.column ?? "")}</b> by <b>{String(obj.method ?? "")}</b>: outside
    <code>{formatNumber(num(obj.lower_bound) ?? -Infinity)}</code> … <code>{formatNumber(num(obj.upper_bound) ?? Infinity)}</code>
  </p>
  {@render table(list(obj.columns).map(String), list(obj.rows) as Row[], obj.truncated === true)}
{:else if tool === "summary_stats"}
  <p class="stats-line"><b>{String(obj.column ?? "")}</b></p>
  <dl class="stats">
    {#each STAT_KEYS as k (k)}
      <div><dt>{k}</dt><dd>{formatCell(obj[k] ?? null)}</dd></div>
    {/each}
  </dl>
  {#if box}
    <svg class="box" viewBox="0 0 300 34" aria-label="Box plot">
      <line x1={box.min} x2={box.max} y1="17" y2="17" />
      <line x1={box.min} x2={box.min} y1="10" y2="24" />
      <line x1={box.max} x2={box.max} y1="10" y2="24" />
      <rect x={box.p25} y="7" width={Math.max(1, box.p75 - box.p25)} height="20" rx="3" />
      <line class="median" x1={box.median} x2={box.median} y1="7" y2="27" />
    </svg>
  {/if}
{:else if tool === "calc_distribution"}
  <p class="stats-line"><b>{String(obj.column ?? "")}</b> · {formatCell(obj.count ?? null)} values</p>
  <svg class="hist" viewBox="0 0 300 96" preserveAspectRatio="none" aria-label="Histogram">
    {#each bins as b, i (i)}
      {@const w = 300 / Math.max(1, bins.length)}
      <rect x={i * w + 1} y={84 - (b.count / binMax) * 80} width={Math.max(1, w - 2)} height={(b.count / binMax) * 80} rx="2">
        <title>{formatNumber(b.lower)} – {formatNumber(b.upper)}: {b.count}</title>
      </rect>
    {/each}
  </svg>
  {#if bins.length}
    <div class="hist-axis"><span>{formatNumber(bins[0]!.lower)}</span><span>{formatNumber(bins.at(-1)!.upper)}</span></div>
  {/if}
{:else if tool === "correlate"}
  {@const columns = list(obj.columns).map(String)}
  <div class="table">
    <table class="corr">
      <thead><tr><th></th>{#each columns as c, i (i)}<th>{c}</th>{/each}</tr></thead>
      <tbody>
        {#each list(obj.matrix) as row, r (r)}
          <tr>
            <th>{columns[r] ?? ""}</th>
            {#each list(row) as cell, c (c)}<td class="num" style:background={heat(num(cell))}>{num(cell) === null ? "—" : num(cell)!.toFixed(2)}</td>{/each}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{:else if tool === "group_compare"}
  {@render table(
    [String(obj.group_column ?? "group"), "count", `mean ${String(obj.value_column ?? "")}`, "std"],
    list(obj.groups).map((g) => fields(g)).map((g) => [g.group ?? null, g.count ?? null, g.mean ?? null, g.std ?? null])
  )}
  {#if num(obj.welch_t) !== null}
    <p class="stats-line">
      Welch's t = <code>{num(obj.welch_t)!.toFixed(2)}</code>{#if num(obj.welch_df) !== null}, df ≈ <code>{num(obj.welch_df)!.toFixed(1)}</code>{/if}
      · {Math.abs(num(obj.welch_t)!) > 2 ? "likely a real difference" : "not clearly different"}
    </p>
  {/if}
{:else if tool === "list_tables"}
  {@render table(["database", "table", "rows"], list(result).map((t) => fields(t)).map((t) => [t.database ?? null, t.table ?? null, t.rows ?? null]))}
{:else if tool === "describe_table"}
  {@render table(["column", "type", "nullable"], list(obj.columns).map((c) => fields(c)).map((c) => [c.name ?? null, c.type ?? null, c.nullable ?? null]))}
{:else}
  <pre class="raw">{raw}</pre>
{/if}

<style>
  .table { max-height: 300px; overflow: auto; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { padding: 4px 10px; text-align: left; border-bottom: 1px solid var(--line); white-space: nowrap; }
  thead th { position: sticky; top: 0; z-index: 1; background: var(--panel-3); font-weight: 600; color: var(--muted); font-family: var(--mono); font-size: 11px; }
  td.num { text-align: right; font-family: var(--mono); }
  td.empty { color: var(--faint); }
  tbody tr:hover td { background: var(--panel-2); }
  .corr tbody th { font-family: var(--mono); font-size: 11px; color: var(--muted); font-weight: 500; }
  .meta { margin: 5px 2px 0; font-size: 11px; color: var(--faint); }
  .stats-line { margin: 0 0 6px; font-size: 12px; color: var(--muted); }
  .stats-line b { color: var(--ink); font-family: var(--mono); font-weight: 500; }
  code { font-size: 11.5px; padding: 0 4px; border-radius: 4px; background: var(--panel-3); }
  .stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(82px, 1fr)); gap: 6px; margin: 0; }
  .stats div { padding: 5px 8px; border: 1px solid var(--line); border-radius: 7px; background: var(--panel); }
  dt { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--faint); }
  dd { margin: 0; font: 12.5px var(--mono); }
  .box { display: block; width: 100%; max-width: 420px; height: 34px; margin-top: 8px; }
  .box line { stroke: var(--muted); stroke-width: 1.5; }
  .box rect { fill: color-mix(in srgb, var(--series-1) 30%, transparent); stroke: var(--series-1); stroke-width: 1.5; }
  .box .median { stroke: var(--ink); stroke-width: 2; }
  .hist { display: block; width: 100%; height: 96px; }
  .hist rect { fill: var(--series-1); }
  .hist rect:hover { fill: color-mix(in srgb, var(--series-1) 70%, var(--ink)); }
  .hist-axis { display: flex; justify-content: space-between; font: 10.5px var(--mono); color: var(--faint); }
  .raw { margin: 0; padding: 8px 10px; max-height: 240px; overflow: auto; white-space: pre-wrap; font-size: 11.5px; background: var(--panel); border: 1px solid var(--line); border-radius: 7px; color: var(--muted); }
</style>
