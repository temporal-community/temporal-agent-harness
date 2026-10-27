<!-- One conversation with the analyst, and the access panel beside it. The page answers the
     agent's chart tools itself (`callbackTools`): each chart is drawn from the call's own input,
     and the call's id is the chart id the answer places it by. An access request is the
     person's to answer, from the card the conversation shows. A report the agent publishes is
     "hosted" here too: the page answers `publish_report` with a link to the report's route, and
     shows the report over the studio while the route is open. Mounted once per session (keyed by
     its id); a draft with no session yet has no agent and creates the session on its first
     question. -->
<script lang="ts">
  import { AgentSession, type HttpTransport, type ToolCall, type ToolPart } from "@temporalio/agent-harness-svelte";
  import { onMount } from "svelte";
  import { SvelteSet } from "svelte/reactivity";

  import type { AccessRequest, GovernedAnalyticsAgent } from "../client_sdk/GovernedAnalyticsAgent";
  import AccessPanel, { DEFAULT_TABLES, type Decision } from "./AccessPanel.svelte";
  import { CHART_TOOLS, DATA_TOOLS, REPORT_TOOL, chartProblem, dataOrigin, policyOf, readReport, readResult, reportUrl, tableKind } from "./analysis.js";
  import ChatPanel from "./ChatPanel.svelte";
  import ReportView from "./ReportView.svelte";

  interface Props {
    sessionId: string | null;
    transport: HttpTransport;
    title: string;
    temporalUi: string;
    /** A question to send as soon as the session opens: the draft's first message. */
    initialPrompt: string | null;
    /** Create the session for a draft's first question. */
    oncreate: (prompt: string) => void;
    /** The conversation's first question, to title it by. */
    ontitle: (text: string) => void;
    /** The report the page's route opens, if any. */
    report: string | null;
    onopenreport: (reportId: string) => void;
    onclosereport: () => void;
  }
  let { sessionId, transport, title, temporalUi, initialPrompt, oncreate, ontitle, report, onopenreport, onclosereport }: Props = $props();

  // Every chart is drawn from its call's input as soon as it is requested; the answer is the id.
  // Data that can't be drawn goes back as the call's error, for the agent to fix.
  const drawChart = (call: ToolCall) => {
    const problem = chartProblem(call.toolName, call.input);
    if (problem) throw new Error(problem);
    return { chart_id: call.toolId };
  };
  // Publishing is simulated: the report is the call's input, on the session's stream like
  // everything else, and its link is this page's route to it.
  const linkTo = (reportId: string) => (sessionId ? reportUrl(location, sessionId, reportId) : "");
  const publish = (call: ToolCall) => {
    const { problem } = readReport(call.input);
    if (problem) throw new Error(problem);
    return { report_id: call.toolId, url: linkTo(call.toolId) };
  };
  const callbackTools = { ...Object.fromEntries(CHART_TOOLS.map((name) => [name, drawChart])), [REPORT_TOOL]: publish };

  // The parent re-mounts this component for each session, so these props never change here.
  // svelte-ignore state_referenced_locally
  const agent = sessionId
    ? new AgentSession<GovernedAnalyticsAgent>({
        sessionId,
        transport,
        callbackTools,
        // A send made while an attach is still opening can miss its wake-up; poll often so the
        // turn it starts shows up promptly anyway.
        idlePollMs: 1000
      })
    : null;

  // -- actions ----------------------------------------------------------------------------------

  function ask(text: string): void {
    if (agent) void agent.sendMessage("ask", { text });
    else oncreate(text);
  }

  function toggle(table: string, grant: boolean): void {
    if (!agent) return;
    const access = { table } as AccessRequest;
    void agent.sendMessage(grant ? "grant_access" : "revoke_access", access);
  }

  /** Access requests answered from here, so the card goes as soon as it is answered. */
  const answered = new SvelteSet<string>();
  async function answer(toolId: string, access: "Grant for this session" | "Decline"): Promise<void> {
    if (!agent) return;
    answered.add(toolId);
    if (!(await agent.provideToolOutput(toolId, { access }))) answered.delete(toolId);
  }

  function approve(toolId: string, approved: boolean): void {
    void agent?.respondToApproval(toolId, { approved, reason: approved ? "Approved from the studio" : "Denied from the studio" });
  }

  onMount(() => {
    if (initialPrompt) ask(initialPrompt);
  });

  $effect(() => {
    const first = messages.find((m) => m.handler === "ask");
    if (first?.handler === "ask") ontitle(first.input.text);
  });

  // -- derived view state -----------------------------------------------------------------------

  const inFlight = (m: { status: string }) => m.status === "sending" || m.status === "accepted" || m.status === "running";
  const messages = $derived(agent?.messages ?? []);
  // A send the server has admitted can sit in `pending` under its real id until the stream's
  // own copy lands; show it once.
  const pending = $derived((agent?.pending ?? []).filter((p) => !messages.some((m) => m.id === p.id)));
  const all = $derived([...messages, ...pending]);
  const asking = $derived(all.filter((m) => m.handler === "ask"));
  const busy = $derived(!!agent && (agent.agentStatus === "busy" || asking.some(inFlight)));
  const tools = $derived(messages.flatMap((m) => m.parts.filter((p): p is ToolPart => p.type === "tool")));

  const charts = $derived(new Map(tools.filter((p) => CHART_TOOLS.includes(p.toolName)).map((p) => [p.toolId, p])));
  const requests = $derived((agent?.pendingCallbacks ?? []).filter((c) => c.part.toolName === "ask_for_access"));
  const requested = $derived(new Set(requests.filter((r) => !answered.has(r.part.toolId)).map((r) => String(r.part.input.table ?? ""))));
  const tables = $derived(agent?.states.table_access?.tables ?? DEFAULT_TABLES);
  // A grant or revoke on its way: the switch shows where it is going until the board catches up.
  const changing = $derived(
    new Map(
      all.flatMap((m) =>
        (m.handler === "grant_access" || m.handler === "revoke_access") && inFlight(m)
          ? [[m.input.table, m.handler === "grant_access" ? "grant" : "revoke"] as const]
          : []
      )
    )
  );
  // Published reports: the policy let them through, so the call reached this page.
  const published = new Set(["awaiting_callback", "running", "done"]);
  const reports = $derived(new Map(tools.filter((p) => p.toolName === REPORT_TOOL && published.has(p.state)).map((p) => [p.toolId, p])));
  const shownReport = $derived(report === null ? null : (reports.get(report) ?? null));
  const restrictedRead = $derived([
    ...new Set(
      tools.flatMap((p) => {
        const origin = p.state === "done" ? dataOrigin(readResult(p.output)) : null;
        return origin?.restricted ? origin.tablesRead.filter((t) => tableKind(t) !== "open") : [];
      })
    )
  ]);
  const decisions = $derived(
    tools.flatMap((p): Decision[] => {
      if (!DATA_TOOLS.includes(p.toolName) && p.toolName !== REPORT_TOOL) return [];
      const policy = policyOf(p);
      if (policy.verdict !== "approved" && policy.verdict !== "denied") return [];
      return [{ toolId: p.toolId, tool: p.toolName, verdict: policy.verdict, tables: policy.tables, reason: policy.reason }];
    })
  );
  const deniedCount = $derived(decisions.filter((d) => d.verdict === "denied").length);

  let now = $state(Date.now() / 1000);
  $effect(() => {
    if (!busy) return;
    const timer = setInterval(() => (now = Date.now() / 1000), 100);
    return () => clearInterval(timer);
  });
</script>

<main>
  <header class="bar">
    <div class="title">
      <h1>{title}</h1>
      {#if agent && sessionId}
        <span class="conn" data-state={agent.connection}>{agent.connection}</span>
        <a href="{temporalUi}/{encodeURIComponent(sessionId)}" target="_blank" rel="noreferrer">open in Temporal ↗</a>
      {/if}
    </div>
    <div class="stats">
      <span class="pill"><span class="shield"></span> policy-checked</span>
      {#if decisions.length}
        <span class="count">{decisions.length} policy decisions · <b class:bad={deniedCount > 0}>{deniedCount} denied</b></span>
      {/if}
    </div>
  </header>

  {#if agent?.error}
    <div class="banner">{agent.error.message} <button onclick={() => agent.clearError()}>Dismiss</button></div>
  {/if}

  <ChatPanel
    {messages}
    {pending}
    {busy}
    {charts}
    {requests}
    {answered}
    {now}
    onsend={ask}
    onanswer={(id, access) => void answer(id, access)}
    onapprove={approve}
    ondismiss={(id) => agent?.dismiss(id)}
    reportUrl={linkTo}
    {onopenreport}
  />
</main>

<AccessPanel {tables} live={!!agent} {changing} {requested} {decisions} {restrictedRead} ontoggle={toggle} />

{#if report !== null}
  <ReportView
    report={shownReport ? readReport(shownReport.input).report : null}
    missing={shownReport ? null : agent && (messages.length === 0 || agent.connection === "connecting" || agent.connection === "stopped") ? "loading" : "not found"}
    charts={charts}
    url={linkTo(report)}
    onback={onclosereport}
  />
{/if}

<style>
  main { display: flex; flex-direction: column; min-width: 0; min-height: 0; padding: 0 16px 14px; gap: 6px; }
  .bar { height: 50px; flex: none; display: flex; align-items: center; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--line); margin: 0 -16px; padding: 0 16px 0 20px; }
  .title { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
  h1 { margin: 0; font-size: 14.5px; font-weight: 600; letter-spacing: -0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .title a { font-size: 11.5px; color: var(--faint); text-decoration: none; white-space: nowrap; }
  .title a:hover { color: var(--accent); }
  .conn { font: 11px var(--mono); color: var(--faint); display: inline-flex; align-items: center; gap: 5px; }
  .conn::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--faint); }
  .conn[data-state="live"]::before, .conn[data-state="idle"]::before { background: var(--good); }
  .conn[data-state="connecting"]::before, .conn[data-state="reconnecting"]::before { background: var(--warn); animation: pulse 1s infinite; }
  .conn[data-state="error"]::before { background: var(--bad); }

  .stats { display: flex; align-items: center; gap: 10px; flex: none; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 500; color: var(--good); border: 1px solid color-mix(in srgb, var(--good) 35%, transparent); background: color-mix(in srgb, var(--good) 8%, transparent); }
  .count { font: 11px var(--mono); color: var(--muted); }
  .count b { font-weight: 500; }
  .count b.bad { color: var(--bad); }
  .shield { width: 11px; height: 11px; background: currentColor; mask: var(--m) center / contain no-repeat; -webkit-mask: var(--m) center / contain no-repeat; --m: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M8 1l6 2.5V8c0 3.5-2.6 6-6 7-3.4-1-6-3.5-6-7V3.5z'/%3E%3C/svg%3E"); }

  .banner { padding: 8px 12px; border-radius: 8px; border: 1px solid color-mix(in srgb, var(--bad) 50%, transparent); background: color-mix(in srgb, var(--bad) 10%, transparent); color: var(--bad); font-size: 12.5px; }
  .banner button { margin-left: 8px; border: 1px solid var(--line); background: var(--panel); border-radius: 5px; font-size: 11.5px; }

  @media (max-width: 900px) {
    main { height: 100vh; }
  }
</style>
