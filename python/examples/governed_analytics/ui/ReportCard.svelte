<!-- One `publish_report` call in the conversation. The tool policy judges it first: while it does,
     a line says so; refused, the card says why; published, it is the report's card, with its
     public link to copy and a click that opens the report. -->
<script lang="ts">
  import type { ToolPart } from "@temporalio/agent-harness-svelte";

  import { policyOf, readReport } from "./analysis.js";

  interface Props {
    part: ToolPart;
    url: string;
    onopen: (reportId: string) => void;
    onapprove: (toolId: string, approved: boolean) => void;
  }
  let { part, url, onopen, onapprove }: Props = $props();

  const policy = $derived(policyOf(part));
  const read = $derived(readReport(part.input));
  const title = $derived(typeof part.input.title === "string" ? part.input.title : "Untitled report");
  const published = $derived((policy.verdict === "approved" || policy.verdict === "not checked") && (part.state === "awaiting_callback" || part.state === "running" || part.state === "done"));

  let copied = $state(false);
  async function copy(e: MouseEvent): Promise<void> {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
      setTimeout(() => (copied = false), 1600);
    } catch {
      // No clipboard (an insecure origin): the link is on the card to select by hand.
    }
  }
</script>

{#if policy.verdict === "denied"}
  <div class="card refused">
    <div class="eyebrow"><span class="shield"></span> Publishing refused by the tool policy</div>
    <b class="title">{title}</b>
    <p class="reason">{(policy.reason ?? "Denied by the tool policy.").split(/\n{2,}/)[0]}</p>
    <small>Nothing was published. {#if policy.decision}<code>{policy.decision}</code>{/if}</small>
  </div>
{:else if policy.verdict === "checking" || part.state === "requested"}
  <div class="card pending"><i class="spinner"></i> Publishing <b>{title}</b> — the tool policy is checking it…</div>
{:else if policy.verdict === "needs you"}
  <div class="card pending">
    The policy couldn't decide whether <b>{title}</b> may be published.
    <span class="actions">
      <button class="primary" onclick={() => onapprove(part.toolId, true)}>Publish</button>
      <button onclick={() => onapprove(part.toolId, false)}>Don't</button>
    </span>
  </div>
{:else if part.state === "failed"}
  <div class="card refused">
    <div class="eyebrow">Couldn't publish the report</div>
    <p class="reason">{part.error}</p>
  </div>
{:else if published && read.report}
  {@const report = read.report}
  <div
    class="card report"
    role="link"
    tabindex="0"
    onclick={() => onopen(part.toolId)}
    onkeydown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onopen(part.toolId))}
  >
    <div class="eyebrow"><span class="globe"></span> Published report · public link</div>
    <b class="title">{report.title}</b>
    {#if report.summary}<p class="summary">{report.summary}</p>{/if}
    <div class="sections">
      {#each report.sections.slice(0, 4) as s, i (i)}<span>{s.heading}</span>{/each}
      {#if report.sections.length > 4}<span>+{report.sections.length - 4} more</span>{/if}
    </div>
    <div class="link">
      <code title={url}>{url}</code>
      <button onclick={copy}>{copied ? "Copied" : "Copy link"}</button>
      <span class="open">Open report ↗</span>
    </div>
  </div>
{/if}

<style>
  .card { padding: 11px 13px; border-radius: var(--radius); border: 1px solid var(--line); background: var(--panel-2); font-size: 12.5px; animation: rise 0.25s ease-out; }
  .pending { display: flex; align-items: center; gap: 8px; color: var(--muted); flex-wrap: wrap; }
  .pending b { color: var(--ink); font-weight: 500; }
  .actions { display: inline-flex; gap: 6px; margin-left: auto; }
  .actions button { font-size: 11.5px; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--line-2); background: var(--panel); }
  .actions .primary { border: 0; background: var(--accent); color: var(--accent-ink); font-weight: 600; }
  .eyebrow { display: flex; align-items: center; gap: 6px; font-size: 10.5px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent); }
  .title { display: block; margin: 5px 0 2px; font-size: 15px; letter-spacing: -0.01em; }

  .refused { border-color: color-mix(in srgb, var(--bad) 55%, var(--line)); background: color-mix(in srgb, var(--bad) 7%, var(--panel-2)); }
  .refused .eyebrow { color: var(--bad); }
  .refused .title { color: var(--muted); text-decoration: line-through; text-decoration-color: color-mix(in srgb, var(--bad) 60%, transparent); }
  .reason { margin: 4px 0 6px; color: var(--ink); font-size: 13px; white-space: pre-wrap; }
  .refused small { color: var(--faint); font-size: 11px; }
  .refused code { font-size: 10.5px; }

  .report { cursor: pointer; border-color: color-mix(in srgb, var(--accent) 40%, var(--line)); background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 9%, var(--panel-2)), var(--panel-2) 60%); transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s; }
  .report:hover { transform: translateY(-1px); border-color: color-mix(in srgb, var(--accent) 70%, transparent); box-shadow: var(--shadow); }
  .summary { margin: 2px 0 8px; color: var(--muted); display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .sections { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }
  .sections span { font-size: 11px; padding: 1px 8px; border-radius: 999px; background: var(--panel-3); color: var(--muted); }
  .link { display: flex; align-items: center; gap: 8px; padding-top: 9px; border-top: 1px solid var(--line); }
  .link code { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; color: var(--faint); }
  .link button { font-size: 11.5px; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--line-2); background: var(--panel); white-space: nowrap; }
  .link button:hover { border-color: var(--faint); }
  .open { font-size: 12px; font-weight: 600; color: var(--accent); white-space: nowrap; }

  .shield, .globe { width: 12px; height: 12px; background: currentColor; mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; }
  .shield { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M8 1l6 2.5V8c0 3.5-2.6 6-6 7-3.4-1-6-3.5-6-7V3.5z'/%3E%3C/svg%3E"); }
  .globe { --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='black' stroke-width='1.5'%3E%3Ccircle cx='8' cy='8' r='6.2'/%3E%3Cpath d='M1.8 8h12.4M8 1.8c2 2 2 10.4 0 12.4M8 1.8c-2 2-2 10.4 0 12.4'/%3E%3C/svg%3E"); }
  .spinner { display: inline-block; width: 10px; height: 10px; border-radius: 50%; border: 2px solid currentColor; border-right-color: transparent; animation: spin 0.7s linear infinite; }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); } }
</style>
