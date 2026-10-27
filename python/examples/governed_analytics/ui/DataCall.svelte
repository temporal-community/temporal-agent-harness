<!-- One data tool call a script made, as a row that opens: its query, what the tool policy decided
     and why, the tables it reads, and what came back. A denied call shows the policy's reason
     without opening. -->
<script module lang="ts">
  export const TOOL_LABELS: Record<string, string> = {
    run_sql: "SQL",
    summary_stats: "Stats",
    calc_distribution: "Distribution",
    correlate: "Correlation",
    group_compare: "Groups",
    detect_outliers: "Outliers",
    list_tables: "Catalog",
    describe_table: "Schema",
    publish_report: "Report"
  };
</script>

<script lang="ts">
  import type { ToolPart } from "@temporalio/agent-harness-svelte";

  import { dataOrigin, highlightSql, policyOf, readResult, tableKind } from "./analysis.js";
  import ResultView from "./ResultView.svelte";

  interface Props {
    part: ToolPart;
    onapprove: (toolId: string, approved: boolean) => void;
  }
  let { part, onapprove }: Props = $props();

  let open = $state(false);

  const policy = $derived(policyOf(part));
  const sql = $derived(typeof part.input.sql === "string" ? part.input.sql : null);
  const params = $derived(Object.entries(part.input).filter(([k]) => k !== "sql"));
  const result = $derived(part.state === "done" ? readResult(part.output) : undefined);
  // What the result says it read: restricted data rules out publishing this session's findings.
  const origin = $derived(dataOrigin(result));
  const subject = $derived(
    sql
      ? sql.replace(/\s+/g, " ").trim()
      : part.toolName === "describe_table"
        ? String(part.input.table ?? "")
        : part.toolName === "list_tables"
          ? `tables in ${String(part.input.database ?? "")}`
          : part.toolName
  );
  // The tables it reads: the policy's view of the query, or the one table a lookup names.
  const tables = $derived(
    policy.tables.length ? policy.tables : part.toolName === "describe_table" && part.input.table ? [String(part.input.table)] : []
  );
  const outcome = $derived.by(() => {
    if (part.state === "failed") return "error";
    if (part.state !== "done") return null;
    if (result && typeof result === "object" && !Array.isArray(result) && Array.isArray(result.rows)) {
      const n = result.rows.length;
      return `${n}${result.truncated === true ? "+" : ""} row${n === 1 ? "" : "s"}`;
    }
    if (Array.isArray(result)) return `${result.length} item${result.length === 1 ? "" : "s"}`;
    return "done";
  });
  const running = $derived(part.state === "running" || part.state === "approved" || part.state === "requested");
  const firstParagraph = (s: string | null) => (s ?? "").split(/\n{2,}/)[0] ?? "";
</script>

<div class="call" data-verdict={policy.verdict} data-state={part.state} class:open>
  <button class="head" onclick={() => (open = !open)} aria-expanded={open}>
    <span class="kind">{TOOL_LABELS[part.toolName] ?? part.toolName}</span>
    <code class="subject">{subject}</code>
    <span class="tables">
      {#each tables.slice(0, 3) as t (t)}<span class="table" data-kind={tableKind(t)}>{t}</span>{/each}
      {#if tables.length > 3}<span class="table">+{tables.length - 3}</span>{/if}
      {#if origin?.restricted}<span class="restricted" title="This result holds restricted data">restricted data</span>{/if}
    </span>
    <span class="verdict">
      {#if policy.verdict === "checking"}<i class="spinner"></i> checking
      {:else if policy.verdict === "approved"}<span class="shield"></span> approved
      {:else if policy.verdict === "denied"}<span class="shield"></span> denied
      {:else if policy.verdict === "needs you"}<span class="shield"></span> needs you
      {:else}—{/if}
    </span>
    <span class="outcome" class:bad={part.state === "failed"}>
      {#if running && policy.verdict !== "checking"}<i class="spinner"></i>{:else}{outcome ?? ""}{/if}
    </span>
    <svg class="chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4" /></svg>
  </button>

  {#if policy.verdict === "denied" && !open}
    <p class="why">{firstParagraph(policy.reason) || "Denied by the tool policy."}</p>
  {/if}
  {#if policy.verdict === "needs you"}
    <div class="ask">
      <span>The policy couldn't decide this call{policy.reason ? `: ${policy.reason}` : ""}. Run it anyway?</span>
      <button class="approve" onclick={() => onapprove(part.toolId, true)}>Approve</button>
      <button onclick={() => onapprove(part.toolId, false)}>Deny</button>
    </div>
  {/if}

  {#if open}
    <div class="body">
      {#if sql}
        <h5>Query</h5>
        <pre class="sql">{@html highlightSql(sql)}</pre>
      {/if}
      {#if params.length}
        <div class="params">
          {#each params as [k, v] (k)}<span><b>{k}</b> {typeof v === "string" ? v : JSON.stringify(v)}</span>{/each}
        </div>
      {/if}

      <h5>Policy</h5>
      <dl class="policy">
        <div><dt>Decision</dt><dd class="v">{policy.verdict}{#if policy.decision} <small>{policy.decision}</small>{/if}</dd></div>
        {#if policy.action}<div><dt>Asked</dt><dd><code>{policy.action}</code></dd></div>{/if}
        {#if policy.statement}<div><dt>Statement</dt><dd><code>{policy.statement}</code></dd></div>{/if}
        {#if tables.length}
          <div><dt>Reads</dt><dd>{#each tables as t (t)}<span class="table" data-kind={tableKind(t)}>{t}</span>{/each}</dd></div>
        {/if}
        {#if policy.tableFunctions.length}
          <div><dt>Functions</dt><dd>{#each policy.tableFunctions as f (f)}<code>{f}</code> {/each}</dd></div>
        {/if}
        {#if policy.checks.length}
          <div><dt>Checks</dt><dd>{#each policy.checks as c, i (i)}<code>{c.action.split("/").at(-1)}({c.table})</code> {/each}</dd></div>
        {/if}
        {#if policy.queryError}<div><dt>Query error</dt><dd class="err">{policy.queryError}</dd></div>{/if}
        {#if policy.reason}<div class="wide"><dt>Reason</dt><dd class="reason">{policy.reason}</dd></div>{/if}
      </dl>

      {#if part.state === "done"}
        <h5>Result</h5>
        {#if origin}
          <p class="origin" class:restricted={origin.restricted}>
            Read {#each origin.tablesRead as t (t)}<span class="table" data-kind={tableKind(t)}>{t}</span>{/each}
            · {origin.restricted ? "holds restricted data, so this session's findings can't be published" : "analytics data only"}
          </p>
        {/if}
        <ResultView tool={part.toolName} {result} raw={part.output ?? ""} />
      {:else if part.state === "failed" && policy.verdict !== "denied"}
        <h5>Error</h5>
        <pre class="error">{part.error}</pre>
      {/if}
    </div>
  {/if}
</div>

<style>
  .call { border: 1px solid var(--line); border-radius: 8px; background: var(--panel-2); animation: rise 0.25s ease-out; overflow: hidden; }
  .call[data-verdict="denied"] { border-color: color-mix(in srgb, var(--bad) 45%, var(--line)); background: color-mix(in srgb, var(--bad) 5%, var(--panel-2)); }
  .call[data-verdict="needs you"] { border-color: color-mix(in srgb, var(--warn) 55%, var(--line)); }
  .head {
    width: 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto auto 64px 12px;
    align-items: center;
    gap: 9px;
    padding: 6px 9px;
    border: 0;
    background: transparent;
    text-align: left;
    font-size: 12px;
  }
  .head:hover { background: var(--panel-3); }
  .kind { font: 600 10px var(--mono); letter-spacing: 0.04em; text-transform: uppercase; padding: 2px 6px; border-radius: 5px; background: var(--accent-soft); color: var(--accent); }
  .subject { font-size: 11.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tables { display: flex; gap: 4px; }
  .table { font: 10.5px var(--mono); padding: 1px 6px; border-radius: 999px; border: 1px solid var(--line-2); color: var(--muted); white-space: nowrap; }
  .table[data-kind="restricted"] { border-color: color-mix(in srgb, var(--warn) 55%, transparent); color: var(--warn); }
  .table[data-kind="pii"] { border-color: color-mix(in srgb, var(--bad) 55%, transparent); color: var(--bad); }
  .tables .restricted { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 6px; border-radius: 4px; white-space: nowrap; color: var(--warn); background: color-mix(in srgb, var(--warn) 14%, transparent); }
  .origin { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; margin: 0 0 6px; font-size: 11.5px; color: var(--faint); }
  .origin.restricted { color: var(--warn); }
  .verdict { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; white-space: nowrap; color: var(--faint); }
  [data-verdict="approved"] .verdict { color: var(--good); }
  [data-verdict="denied"] .verdict { color: var(--bad); }
  [data-verdict="needs you"] .verdict, [data-verdict="checking"] .verdict { color: var(--warn); }
  .shield { width: 12px; height: 12px; background: currentColor; mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M8 1l6 2.5V8c0 3.5-2.6 6-6 7-3.4-1-6-3.5-6-7V3.5z'/%3E%3C/svg%3E"); }
  .outcome { font: 11px var(--mono); color: var(--faint); text-align: right; white-space: nowrap; }
  .outcome.bad { color: var(--bad); }
  .chev { width: 12px; height: 12px; fill: none; stroke: var(--faint); stroke-width: 1.8; transition: transform 0.15s; }
  .open .chev { transform: rotate(90deg); }

  .why { margin: 0; padding: 0 10px 7px; font-size: 12px; color: var(--bad); white-space: pre-wrap; }
  .ask { display: flex; align-items: center; gap: 8px; padding: 0 10px 8px; font-size: 12px; color: var(--warn); }
  .ask span { flex: 1; }
  .ask button { font-size: 11.5px; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--line-2); background: var(--panel); color: var(--ink); }
  .ask .approve { border: 0; background: var(--accent); color: var(--accent-ink); font-weight: 600; }

  .body { padding: 2px 12px 12px; border-top: 1px solid var(--line); background: var(--panel); }
  h5 { margin: 12px 0 5px; font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); }
  .sql { margin: 0; padding: 9px 11px; border-radius: 7px; background: var(--panel-2); border: 1px solid var(--line); white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; line-height: 1.55; }
  .params { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }
  .params span { font: 11px var(--mono); padding: 2px 7px; border-radius: 5px; background: var(--panel-3); color: var(--muted); }
  .params b { color: var(--ink); font-weight: 500; }
  .policy { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 6px 14px; margin: 0; }
  .policy .wide { grid-column: 1 / -1; }
  dt { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--faint); }
  dd { margin: 2px 0 0; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; font-size: 12px; }
  dd code { font-size: 11px; padding: 0 5px; border-radius: 4px; background: var(--panel-3); }
  dd small { color: var(--faint); font: 10.5px var(--mono); }
  .v { font-weight: 600; text-transform: capitalize; }
  [data-verdict="approved"] .v { color: var(--good); }
  [data-verdict="denied"] .v { color: var(--bad); }
  .reason { display: block; white-space: pre-wrap; color: var(--muted); }
  .err, .error { color: var(--bad); }
  .error { margin: 0; padding: 8px 10px; white-space: pre-wrap; font-size: 11.5px; background: var(--panel-2); border: 1px solid var(--line); border-radius: 7px; }

  .spinner { display: inline-block; width: 10px; height: 10px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.7s linear infinite; }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); } }
  @media (max-width: 1180px) {
    .tables .table { display: none; }
  }
</style>
