<!-- Governance at a glance: which tables the analyst may read right now (the agent's `table_access`
     state, live), with a switch per restricted table that sends `grant_access` or
     `revoke_access`, and every decision the tool policy made in this session. -->
<script module lang="ts">
  import type { TableAccess } from "../client_sdk/GovernedAnalyticsAgent";

  /** The board before the session publishes its own. */
  export const DEFAULT_TABLES: TableAccess[] = [
    { table: "risk.loan_applications", status: "needs access", via: null, reason: null },
    { table: "risk.credit_scores", status: "needs access", via: null, reason: null },
    { table: "customer_pii.customers", status: "never", via: "the tool policy", reason: null }
  ];

  export interface Decision {
    toolId: string;
    tool: string;
    verdict: "approved" | "denied";
    tables: string[];
    reason: string | null;
  }

  const STATUS_LABELS: Record<TableAccess["status"], string> = {
    "needs access": "Needs access",
    granted: "Granted",
    declined: "Declined",
    revoked: "Revoked",
    never: "Never"
  };
</script>

<script lang="ts">
  import { tableKind } from "./analysis.js";
  import { TOOL_LABELS } from "./DataCall.svelte";

  interface Props {
    tables: readonly TableAccess[];
    /** Whether there is a session to send grants to. */
    live: boolean;
    /** Tables with a grant or revoke on its way, and which. */
    changing: ReadonlyMap<string, "grant" | "revoke">;
    /** Tables the agent is waiting on an answer for. */
    requested: ReadonlySet<string>;
    decisions: readonly Decision[];
    /** Restricted tables whose data this session has read: the policy refuses a public report
     *  after any. */
    restrictedRead: readonly string[];
    ontoggle: (table: string, grant: boolean) => void;
  }
  let { tables, live, changing, requested, decisions, restrictedRead, ontoggle }: Props = $props();

  const approved = $derived(decisions.filter((d) => d.verdict === "approved").length);
  const denied = $derived(decisions.length - approved);
  const recent = $derived([...decisions].reverse());
</script>

<aside>
  <header>
    <b>Data access</b>
    <span>What the analyst may read, right now</span>
  </header>

  <div class="scroll">
    <section>
      <div class="row open">
        <div class="name">
          <code>analytics.*</code>
          <span class="kind">Open</span>
        </div>
        <span class="status" data-status="granted">Always</span>
        <p class="via">Branches, deposits, products and transactions.</p>
      </div>

      {#each tables as t (t.table)}
        {@const kind = tableKind(t.table)}
        {@const pending = changing.get(t.table)}
        {@const on = pending ? pending === "grant" : t.status === "granted"}
        <div class="row" data-status={t.status} class:asked={requested.has(t.table)}>
          <div class="name">
            <code>{t.table}</code>
            <span class="kind" data-kind={kind}>{kind === "pii" ? "PII" : "Restricted"}</span>
          </div>
          {#if t.status === "never"}
            <span class="status" data-status="never"><span class="lock"></span> Never</span>
          {:else}
            <button
              class="switch"
              role="switch"
              aria-checked={on}
              aria-label="{on ? 'Revoke' : 'Grant'} access to {t.table}"
              class:on
              class:busy={!!pending}
              disabled={!live || !!pending}
              title={live ? (on ? "Revoke access" : "Grant access for this session") : "Ask a question first"}
              onclick={() => ontoggle(t.table, !on)}
            ><i></i></button>
          {/if}
          <p class="via">
            {#if requested.has(t.table)}
              <b class="ask">The analyst is asking for access — answer below the conversation.</b>
            {:else if t.status === "never"}
              Refused by the tool policy, whatever is granted.
            {:else}
              <span class="label" data-status={t.status}>{STATUS_LABELS[t.status]}</span>{#if t.via} · via {t.via}{/if}
            {/if}
            {#if t.reason && !requested.has(t.table)}<q>{t.reason}</q>{/if}
          </p>
        </div>
      {/each}
    </section>

    <section>
      <div class="row publishing" data-status={restrictedRead.length ? "blocked" : "allowed"}>
        <div class="name"><span class="globe"></span> Public reports</div>
        <span class="status" data-status={restrictedRead.length ? "never" : "granted"}>{restrictedRead.length ? "Blocked" : "Allowed"}</span>
        <p class="via">
          {#if restrictedRead.length}
            This session read restricted data ({#each restrictedRead as t, i (t)}{i ? ", " : ""}<code>{t}</code>{/each}), so the policy refuses to publish its findings.
          {:else}
            Nothing restricted has been read, so the analyst may publish a report.
          {/if}
        </p>
      </div>
    </section>

    <section class="decisions">
      <h3>
        Policy decisions
        <span><b class="ok">{approved}</b> approved · <b class="bad">{denied}</b> denied</span>
      </h3>
      {#each recent as d (d.toolId)}
        <div class="decision" data-verdict={d.verdict}>
          <span class="mark">{d.verdict === "approved" ? "✓" : "✕"}</span>
          <div>
            <div class="what">
              <span class="tool">{TOOL_LABELS[d.tool] ?? d.tool}</span>
              {#each d.tables as t (t)}<code data-kind={tableKind(t)}>{t}</code>{/each}
            </div>
            {#if d.verdict === "denied" && d.reason}<p>{d.reason.split(/\n{2,}/)[0]}</p>{/if}
          </div>
        </div>
      {:else}
        <p class="none">Each data call the analyst makes is put to the Dogwood policy first. Its decisions will appear here.</p>
      {/each}
    </section>
  </div>

  <footer>Decided by <code>policies/bank.dw</code>, recorded in the workflow's history.</footer>
</aside>

<style>
  aside { display: flex; flex-direction: column; min-height: 0; background: var(--panel); border-left: 1px solid var(--line); }
  header { height: 50px; flex: none; display: flex; flex-direction: column; justify-content: center; padding: 0 16px; border-bottom: 1px solid var(--line); }
  header b { font-size: 13.5px; }
  header span { font-size: 11.5px; color: var(--faint); }
  .scroll { flex: 1; overflow: auto; min-height: 0; padding: 12px; display: flex; flex-direction: column; gap: 18px; }
  section { display: flex; flex-direction: column; gap: 8px; }

  .row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 4px 10px;
    align-items: center;
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: var(--radius);
    background: var(--panel-2);
    transition: border-color 0.2s, box-shadow 0.2s;
  }
  .row[data-status="granted"] { border-color: color-mix(in srgb, var(--good) 45%, var(--line)); }
  .row.asked { border-color: color-mix(in srgb, var(--warn) 65%, var(--line)); animation: glow 1.6s ease-in-out infinite; }
  .row.open { background: transparent; border-style: dashed; }
  .row.publishing[data-status="blocked"] { border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }
  .publishing .name { font-size: 12.5px; font-weight: 600; }
  .publishing .via code { font-size: 10.5px; color: var(--warn); }
  .globe { width: 13px; height: 13px; flex: none; background: var(--muted); mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='none' stroke='black' stroke-width='1.5'%3E%3Ccircle cx='8' cy='8' r='6.2'/%3E%3Cpath d='M1.8 8h12.4M8 1.8c2 2 2 10.4 0 12.4M8 1.8c-2 2-2 10.4 0 12.4'/%3E%3C/svg%3E"); }
  .name { display: flex; align-items: center; gap: 7px; min-width: 0; }
  .name code { font-size: 12px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .kind { font-size: 9.5px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; padding: 1px 5px; border-radius: 4px; color: var(--good); background: color-mix(in srgb, var(--good) 12%, transparent); }
  .kind[data-kind="restricted"] { color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, transparent); }
  .kind[data-kind="pii"] { color: var(--bad); background: color-mix(in srgb, var(--bad) 12%, transparent); }
  .via { grid-column: 1 / -1; margin: 0; font-size: 11.5px; color: var(--faint); }
  .via q { display: block; margin-top: 2px; font-style: italic; color: var(--muted); }
  .label { font-weight: 600; color: var(--muted); }
  .label[data-status="granted"] { color: var(--good); }
  .label[data-status="declined"], .label[data-status="revoked"] { color: var(--bad); }
  .ask { color: var(--warn); font-weight: 600; }
  .status { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600; color: var(--good); }
  .status[data-status="never"] { color: var(--bad); }
  .lock { width: 11px; height: 11px; background: currentColor; mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M4 7V5a4 4 0 0 1 8 0v2h1v8H3V7zm2 0h4V5a2 2 0 0 0-4 0z'/%3E%3C/svg%3E"); }

  .switch { position: relative; width: 38px; height: 22px; padding: 0; border-radius: 999px; border: 1px solid var(--line-2); background: var(--panel-3); transition: background 0.2s, border-color 0.2s; }
  .switch i { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: var(--muted); transition: transform 0.2s, background 0.2s; }
  .switch.on { background: color-mix(in srgb, var(--good) 75%, transparent); border-color: transparent; }
  .switch.on i { transform: translateX(16px); background: white; }
  .switch.busy i { animation: pulse 0.8s infinite; }
  .switch:disabled { cursor: default; }
  .switch:disabled:not(.busy) { opacity: 0.45; }

  h3 { margin: 0; font-size: 10.5px; font-weight: 600; letter-spacing: 0.09em; text-transform: uppercase; color: var(--faint); display: flex; justify-content: space-between; }
  h3 span { text-transform: none; letter-spacing: 0; font-weight: 400; font-size: 11px; }
  .ok { color: var(--good); }
  .bad { color: var(--bad); }
  .decision { display: grid; grid-template-columns: 18px minmax(0, 1fr); gap: 8px; padding: 7px 9px; border-radius: 8px; background: var(--panel-2); border: 1px solid var(--line); animation: rise 0.25s ease-out; }
  .decision[data-verdict="denied"] { border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }
  .mark { width: 18px; height: 18px; border-radius: 5px; display: grid; place-items: center; font-size: 10px; font-weight: 700; color: var(--good); background: color-mix(in srgb, var(--good) 14%, transparent); }
  [data-verdict="denied"] .mark { color: var(--bad); background: color-mix(in srgb, var(--bad) 14%, transparent); }
  .what { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
  .tool { font: 600 10px var(--mono); text-transform: uppercase; color: var(--muted); margin-right: 2px; }
  .what code { font-size: 10.5px; padding: 0 5px; border-radius: 999px; border: 1px solid var(--line-2); color: var(--muted); }
  .what code[data-kind="restricted"] { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 50%, transparent); }
  .what code[data-kind="pii"] { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 50%, transparent); }
  .decision p { margin: 3px 0 0; font-size: 11.5px; color: var(--muted); }
  .none { margin: 0; font-size: 12px; color: var(--faint); }

  footer { padding: 9px 14px; border-top: 1px solid var(--line); color: var(--faint); font-size: 11px; }
  footer code { font-size: 10.5px; }
  @keyframes glow { 50% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--warn) 18%, transparent); } }
  @keyframes rise { from { opacity: 0; transform: translateY(4px); } }
</style>
