<!-- One `run_analysis` call: the script the model wrote, and each host call it made, in order.
     Data calls are DataCall rows; charts draw here as soon as they are requested, folding away
     once the answer places them; an access request shows how the person answered. -->
<script lang="ts">
  import type { ToolPart } from "@temporalio/agent-harness-svelte";

  import { CHART_TOOLS, DATA_TOOLS, REPORT_TOOL, highlightPython, policyOf, readResult, readScriptOutput } from "./analysis.js";
  import Chart, { CHART_KINDS } from "./Chart.svelte";
  import DataCall from "./DataCall.svelte";
  import ReportCard from "./ReportCard.svelte";

  interface Props {
    /** The `run_analysis` call; null for host calls that arrived without one. */
    script: ToolPart | null;
    calls: readonly ToolPart[];
    /** Charts the answer places itself, so they needn't show here as well. */
    placed: ReadonlySet<string>;
    onapprove: (toolId: string, approved: boolean) => void;
    /** A published report's public link. */
    reportUrl: (reportId: string) => string;
    onopenreport: (reportId: string) => void;
  }
  let { script, calls, placed, onapprove, reportUrl, onopenreport }: Props = $props();

  let showScript = $state(false);

  const data = $derived(calls.filter((c) => DATA_TOOLS.includes(c.toolName)));
  const denied = $derived(calls.filter((c) => (DATA_TOOLS.includes(c.toolName) || c.toolName === REPORT_TOOL) && policyOf(c).verdict === "denied").length);
  const running = $derived(script !== null && script.state !== "done" && script.state !== "failed" && script.state !== "denied");
  const output = $derived(script?.state === "done" && script.output ? readScriptOutput(script.output) : null);
  const failure = $derived(script?.state === "failed" ? script.error : (output?.error ?? null));
  const source = $derived(String(script?.input.script ?? ""));
  const lineNumbers = $derived(source.split("\n").map((_, i) => i + 1).join("\n"));

  function accessAnswer(c: ToolPart): "granted" | "declined" | "waiting" {
    if (c.state === "done") {
      const result = readResult(c.output);
      const access = result && typeof result === "object" && !Array.isArray(result) ? result.access : null;
      return access === "Grant for this session" ? "granted" : "declined";
    }
    return c.state === "failed" ? "declined" : "waiting";
  }
</script>

<div class="analysis" class:running>
  <button class="head" onclick={() => (showScript = !showScript)} aria-expanded={showScript} disabled={!script}>
    <span class="icon">{#if running}<i class="spinner"></i>{:else if failure}!{:else}ƒ{/if}</span>
    <span class="title">
      {running ? "Running an analysis" : failure ? "The analysis failed" : "Ran an analysis"}
      <small>· {data.length} data call{data.length === 1 ? "" : "s"}{#if denied} · <b>{denied} denied</b>{/if}</small>
    </span>
    {#if script}<span class="toggle">{showScript ? "Hide script" : "Show script"}</span>{/if}
  </button>

  {#if showScript && script}
    <div class="script">
      <div class="source"><div class="lines"><pre class="gutter" aria-hidden="true">{lineNumbers}</pre><pre class="code">{@html highlightPython(source)}</pre></div></div>
      {#if output?.printed}<h5>Printed</h5><pre class="out">{output.printed}</pre>{/if}
      {#if output?.result}<h5>Returned</h5><pre class="out">{output.result}</pre>{/if}
    </div>
  {/if}
  {#if failure}
    <pre class="failure">{failure}</pre>
  {/if}

  <div class="calls">
    {#each calls as c (c.toolId)}
      {#if DATA_TOOLS.includes(c.toolName)}
        <DataCall part={c} {onapprove} />
      {:else if c.toolName === REPORT_TOOL}
        <ReportCard part={c} url={reportUrl(c.toolId)} onopen={onopenreport} {onapprove} />
      {:else if CHART_TOOLS.includes(c.toolName)}
        <details class="drawn" open={!placed.has(c.toolId)}>
          <summary>
            <span class="dot"></span>Drew a {(CHART_KINDS[c.toolName] ?? "chart").toLowerCase()}: <b>{String(c.input.title ?? "")}</b>
            {#if placed.has(c.toolId)}<small>placed in the answer or a report</small>{/if}
          </summary>
          <Chart tool={c.toolName} input={c.input} problem={c.state === "failed" ? c.error : null} />
        </details>
      {:else if c.toolName === "ask_for_access"}
        {@const answer = accessAnswer(c)}
        <div class="access" data-answer={answer}>
          <span class="lock"></span>
          <span>
            {#if answer === "waiting"}Asked you for access to <code>{String(c.input.table ?? "")}</code>, and is waiting for your answer
            {:else if answer === "granted"}You granted access to <code>{String(c.input.table ?? "")}</code> for this session
            {:else}You declined access to <code>{String(c.input.table ?? "")}</code>{/if}
            {#if c.input.reason}<q>{String(c.input.reason)}</q>{/if}
          </span>
        </div>
      {:else if c.toolName !== "request_access"}
        <div class="other"><code>{c.toolName}</code> {c.state}</div>
      {/if}
    {/each}
  </div>
</div>

<style>
  .analysis { display: flex; flex-direction: column; gap: 6px; padding: 8px; border: 1px solid var(--line); border-radius: var(--radius); background: color-mix(in srgb, var(--panel-2) 55%, transparent); }
  .analysis.running { border-color: color-mix(in srgb, var(--cyan) 40%, var(--line)); }
  .head { display: flex; align-items: center; gap: 9px; padding: 2px 2px 2px 0; border: 0; background: transparent; text-align: left; }
  .head:disabled { cursor: default; }
  .icon { width: 22px; height: 22px; flex: none; border-radius: 6px; display: grid; place-items: center; font: italic 600 12px var(--mono); background: color-mix(in srgb, var(--cyan) 15%, transparent); color: var(--cyan); }
  .title { flex: 1; font-weight: 600; font-size: 12.5px; }
  .title small { font-weight: 400; color: var(--faint); font-size: 11.5px; }
  .title b { color: var(--bad); font-weight: 600; }
  .toggle { font-size: 11px; color: var(--faint); }
  .head:hover .toggle { color: var(--accent); }
  .script { border: 1px solid var(--line); border-radius: 7px; background: var(--panel); padding: 8px 10px; }
  .script pre { margin: 0; font-size: 11.5px; line-height: 1.55; }
  /* One scroll box for the numbers and the code, so they scroll together; the numbers stay put
     when long lines scroll sideways. */
  .source { max-height: 420px; overflow: auto; }
  .lines { display: flex; align-items: flex-start; width: max-content; min-width: 100%; }
  .lines pre { white-space: pre; }
  .gutter { position: sticky; left: 0; flex: none; padding-right: 10px; border-right: 1px solid var(--line); background: var(--panel); text-align: right; color: var(--faint); user-select: none; }
  .code { flex: 1; padding: 0 10px; }
  .code :global(.host-call) { color: var(--cyan); font-weight: 600; }
  .script h5 { margin: 10px 0 4px; font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); }
  .script .out { white-space: pre-wrap; max-height: 320px; overflow: auto; color: var(--muted); }
  .failure { margin: 0; padding: 7px 10px; border-radius: 7px; white-space: pre-wrap; font-size: 11.5px; color: var(--bad); background: color-mix(in srgb, var(--bad) 7%, var(--panel)); border: 1px solid color-mix(in srgb, var(--bad) 35%, var(--line)); max-height: 200px; overflow: auto; }
  .calls { display: flex; flex-direction: column; gap: 5px; }

  .drawn summary { display: flex; align-items: center; gap: 8px; padding: 5px 9px; font-size: 12px; color: var(--muted); cursor: pointer; border: 1px solid var(--line); border-radius: 8px; background: var(--panel-2); }
  .drawn summary b { color: var(--ink); font-weight: 500; }
  .drawn summary small { margin-left: auto; font-size: 11px; color: var(--faint); }
  .drawn .dot { width: 8px; height: 8px; border-radius: 2px; background: var(--series-1); }
  .drawn[open] summary { margin-bottom: 6px; }

  .access { display: flex; align-items: flex-start; gap: 9px; padding: 7px 10px; border-radius: 8px; font-size: 12.5px; border: 1px solid var(--line); background: var(--panel-2); color: var(--muted); }
  .access[data-answer="waiting"] { border-color: color-mix(in srgb, var(--warn) 55%, var(--line)); color: var(--ink); animation: glow 1.6s ease-in-out infinite; }
  .access[data-answer="granted"] { border-color: color-mix(in srgb, var(--good) 45%, var(--line)); }
  .access code { font-size: 11.5px; padding: 0 5px; border-radius: 4px; background: var(--panel-3); }
  .access q { display: block; margin-top: 2px; font-style: italic; color: var(--faint); }
  .lock { width: 14px; height: 14px; margin-top: 2px; flex: none; background: var(--warn); mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M4 7V5a4 4 0 0 1 8 0v2h1v8H3V7zm2 0h4V5a2 2 0 0 0-4 0z'/%3E%3C/svg%3E"); }
  [data-answer="granted"] .lock { background: var(--good); }
  [data-answer="declined"] .lock { background: var(--faint); }
  .other { font-size: 11.5px; color: var(--faint); padding: 0 4px; }
  @keyframes glow { 50% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 18%, transparent); } }
  .spinner { display: inline-block; width: 10px; height: 10px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.7s linear infinite; }
</style>
