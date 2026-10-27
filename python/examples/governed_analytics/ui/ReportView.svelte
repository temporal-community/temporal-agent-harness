<!-- A published report, as anyone with its link sees it: the title, the summary and each section,
     its markdown rendered and its `[chart:<id>]` lines drawn from the session's charts. Shown over
     the studio, which stays mounted beneath it, so Back returns to it as it was. -->
<script lang="ts">
  import type { ToolPart } from "@temporalio/agent-harness-svelte";

  import { markdown, splitAnswer, type Report } from "./analysis.js";
  import Chart from "./Chart.svelte";

  interface Props {
    report: Report | null;
    /** Why there is no report to show: still loading, or none by that id. */
    missing: "loading" | "not found" | null;
    charts: ReadonlyMap<string, ToolPart>;
    url: string;
    onback: () => void;
  }
  let { report, missing, charts, url, onback }: Props = $props();

  let copied = $state(false);
  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
      setTimeout(() => (copied = false), 1600);
    } catch {
      // No clipboard (an insecure origin): the address bar has the link.
    }
  }

  function keydown(e: KeyboardEvent): void {
    if (e.key === "Escape") onback();
  }
</script>

<svelte:window onkeydown={keydown} />

<div class="page">
  <header class="bar">
    <button class="back" onclick={onback}>← Back to the studio</button>
    <div class="brand">
      <svg viewBox="0 0 32 32" aria-hidden="true">
        <rect class="frame" x="1" y="1" width="30" height="30" rx="8" />
        <rect class="b" x="8" y="16" width="4" height="8" rx="1" /><rect class="b" x="14" y="11" width="4" height="13" rx="1" /><rect class="b" x="20" y="7" width="4" height="17" rx="1" />
      </svg>
      Governed Analytics <span>· Published report</span>
    </div>
    <div class="share">
      <span class="public"><span class="globe"></span> Public</span>
      <button onclick={copy}>{copied ? "Copied" : "Copy link"}</button>
    </div>
  </header>

  {#if report}
    <article>
      <p class="eyebrow">Report</p>
      <h1>{report.title}</h1>
      <p class="meta">Published from a policy-checked analytics session · {report.sections.length} section{report.sections.length === 1 ? "" : "s"}</p>
      {#if report.summary}<p class="summary">{report.summary}</p>{/if}

      {#if report.sections.length > 2}
        <nav>
          <b>Contents</b>
          <ol>{#each report.sections as s, i (i)}<li><button onclick={() => document.getElementById(`section-${i}`)?.scrollIntoView({ behavior: "smooth" })}>{s.heading}</button></li>{/each}</ol>
        </nav>
      {/if}

      {#each report.sections as s, i (i)}
        <section id="section-{i}">
          <h2>{s.heading}</h2>
          {#each splitAnswer(s.body) as seg, j (j)}
            {#if seg.type === "text"}
              <div class="md">{@html markdown(seg.text)}</div>
            {:else if charts.get(seg.id)}
              {@const c = charts.get(seg.id)!}
              <Chart tool={c.toolName} input={c.input} problem={c.state === "failed" ? c.error : null} />
            {:else}
              <div class="placeholder">This chart isn't available <code>{seg.id}</code></div>
            {/if}
          {/each}
        </section>
      {/each}

      <footer>
        <span class="shield"></span>
        Every query behind this report was checked by the bank's tool policy before it ran, and the session's history records each one.
      </footer>
    </article>
  {:else}
    <div class="empty">
      {#if missing === "loading"}<i class="spinner"></i> Loading the report…{:else}This report doesn't exist, or its session can't be read.{/if}
    </div>
  {/if}
</div>

<style>
  .page { position: fixed; inset: 0; z-index: 20; overflow: auto; background: var(--bg); animation: fade 0.2s ease-out; }
  .bar { position: sticky; top: 0; z-index: 1; height: 54px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 20px; background: color-mix(in srgb, var(--panel) 88%, transparent); backdrop-filter: blur(8px); border-bottom: 1px solid var(--line); }
  .back { border: 1px solid var(--line); background: var(--panel-2); border-radius: 8px; padding: 5px 11px; font-size: 12.5px; color: var(--muted); }
  .back:hover { color: var(--ink); border-color: var(--line-2); }
  .brand { display: flex; align-items: center; gap: 9px; font-weight: 600; font-size: 13.5px; }
  .brand span { font-weight: 400; color: var(--faint); }
  .brand svg { width: 24px; height: 24px; }
  .brand .frame { fill: var(--accent-soft); stroke: color-mix(in srgb, var(--accent) 45%, transparent); }
  .brand .b { fill: var(--accent); }
  .share { display: flex; align-items: center; gap: 8px; }
  .public { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: var(--good); padding: 3px 9px; border-radius: 999px; border: 1px solid color-mix(in srgb, var(--good) 35%, transparent); }
  .share button { font-size: 12px; padding: 5px 12px; border-radius: 8px; border: 0; background: var(--accent); color: var(--accent-ink); font-weight: 600; }

  article { max-width: 820px; margin: 36px auto 60px; padding: 44px 52px 36px; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; box-shadow: var(--shadow); }
  .eyebrow { margin: 0; font-size: 11px; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent); }
  h1 { margin: 8px 0 6px; font-size: 32px; line-height: 1.15; letter-spacing: -0.025em; }
  .meta { margin: 0 0 22px; font-size: 12.5px; color: var(--faint); }
  .summary { margin: 0 0 28px; padding: 14px 18px; font-size: 16px; line-height: 1.6; color: var(--ink); border-left: 3px solid var(--accent); background: var(--accent-soft); border-radius: 0 10px 10px 0; }
  nav { margin: 0 0 28px; font-size: 13px; }
  nav b { font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); }
  nav ol { margin: 6px 0 0; padding-left: 20px; }
  nav button { padding: 1px 0; border: 0; background: none; color: var(--muted); text-align: left; }
  nav button:hover { color: var(--accent); }
  section { margin-top: 30px; scroll-margin-top: 70px; }
  h2 { margin: 0 0 10px; padding-bottom: 8px; font-size: 20px; letter-spacing: -0.015em; border-bottom: 1px solid var(--line); }
  .md { font-size: 14.5px; line-height: 1.7; }
  .md :global(p) { margin: 0 0 12px; }
  .md :global(ul), .md :global(ol) { margin: 0 0 12px; padding-left: 22px; }
  .md :global(code) { font-size: 12.5px; padding: 1px 5px; border-radius: 4px; background: var(--panel-3); }
  .md :global(h4) { margin: 14px 0 6px; font-size: 15px; }
  .md :global(table) { border-collapse: collapse; margin: 4px 0 14px; font-size: 13px; }
  .md :global(th), .md :global(td) { padding: 6px 12px; border: 1px solid var(--line); text-align: left; }
  .md :global(th) { background: var(--panel-3); font-weight: 600; }
  .md :global(a) { color: var(--accent); }
  .placeholder { margin: 8px 0 14px; padding: 26px; border: 1px dashed var(--line-2); border-radius: var(--radius); text-align: center; color: var(--faint); font-size: 13px; }
  .placeholder code { font-size: 11.5px; }
  footer { display: flex; align-items: center; gap: 8px; margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--line); font-size: 12px; color: var(--faint); }
  .empty { display: grid; place-items: center; min-height: 60vh; color: var(--muted); }

  .shield, .globe { width: 12px; height: 12px; flex: none; background: currentColor; mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; }
  .shield { color: var(--good); --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M8 1l6 2.5V8c0 3.5-2.6 6-6 7-3.4-1-6-3.5-6-7V3.5z'/%3E%3C/svg%3E"); }
  .globe { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='black' stroke-width='1.5'%3E%3Ccircle cx='8' cy='8' r='6.2'/%3E%3Cpath d='M1.8 8h12.4M8 1.8c2 2 2 10.4 0 12.4M8 1.8c-2 2-2 10.4 0 12.4'/%3E%3C/svg%3E"); }
  .spinner { display: inline-block; width: 11px; height: 11px; margin-right: 8px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.7s linear infinite; }
  @keyframes fade { from { opacity: 0; } }
  @media (max-width: 760px) {
    article { margin: 0; padding: 28px 18px; border-radius: 0; border-left: 0; border-right: 0; }
    h1 { font-size: 25px; }
    .brand span { display: none; }
  }
</style>
