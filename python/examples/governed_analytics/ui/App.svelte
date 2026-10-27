<!-- Governed Analytics Studio: the session picker, and the studio for the chosen conversation.
     Each session is one GovernedAnalyticsAgent; "New analysis" opens a draft that becomes a
     session on its first question. The URL routes the page: `?session=<id>` picks the session,
     and `#/reports/<id>` shows one of its published reports. A report opens at that link in a
     new tab, so it reads as the public page it stands in for. -->
<script lang="ts">
  import { HttpTransport, startSession, type SessionSummary } from "@temporalio/agent-harness-svelte";

  import type { GovernedAnalyticsAgent } from "../client_sdk/GovernedAnalyticsAgent";
  import { reportRoute, reportUrl } from "./analysis.js";
  import Sidebar from "./Sidebar.svelte";
  import Studio from "./Studio.svelte";

  const params = new URLSearchParams(location.search);
  const API = params.get("api") ?? "http://localhost:8000/api/";
  const TEMPORAL_UI = params.get("temporal-ui") ?? "http://localhost:8233/namespaces/default/workflows";
  const transport = new HttpTransport({ baseUrl: API });

  let sessionId = $state(params.get("session") ?? "");
  let report = $state(reportRoute(location.hash));
  /** Whether the open report was opened from the studio, so Back can return to it. */
  let openedHere = false;
  let initialPrompt = $state<string | null>(null);
  let sessions = $state<SessionSummary[]>([]);
  let apiDown = $state(false);
  let startError = $state<string | null>(null);

  // Titles are the first question of each conversation, kept in this browser.
  let titles = $state<Record<string, string>>(loadTitles());
  function loadTitles(): Record<string, string> {
    try {
      return JSON.parse(localStorage.getItem("governed-analytics:titles") ?? "{}") as Record<string, string>;
    } catch {
      return {};
    }
  }
  function setTitle(id: string, title: string): void {
    titles = { ...titles, [id]: title.length > 60 ? `${title.slice(0, 57)}…` : title };
    try {
      localStorage.setItem("governed-analytics:titles", JSON.stringify(titles));
    } catch {
      // A private window: titles just won't survive a reload.
    }
  }

  async function refreshSessions(): Promise<void> {
    try {
      const all = await transport.listSessions();
      sessions = all.filter((s) => s.agent_workflow_type === "GovernedAnalyticsAgent").sort((a, b) => b.created_at - a.created_at);
      apiDown = false;
    } catch {
      apiDown = true;
    }
  }
  $effect(() => {
    void refreshSessions();
    const timer = setInterval(refreshSessions, 8000);
    return () => clearInterval(timer);
  });

  function open(id: string, prompt: string | null = null): void {
    initialPrompt = prompt;
    sessionId = id;
    report = null;
    history.pushState(null, "", id ? `?session=${encodeURIComponent(id)}` : location.pathname);
  }
  // Back, Forward, and a report link followed in this page: follow the URL.
  $effect(() => {
    const onNavigate = () => {
      const id = new URLSearchParams(location.search).get("session") ?? "";
      if (id !== sessionId) {
        initialPrompt = null;
        sessionId = id;
      }
      const next = reportRoute(location.hash);
      // Arriving at a report from the studio (a card, a link in an answer, Forward): the studio is
      // the history entry behind it.
      if (next !== null && report === null) openedHere = true;
      if (next === null) openedHere = false;
      report = next;
    };
    window.addEventListener("popstate", onNavigate);
    window.addEventListener("hashchange", onNavigate);
    return () => {
      window.removeEventListener("popstate", onNavigate);
      window.removeEventListener("hashchange", onNavigate);
    };
  });

  // A published report opens at its public link in a tab of its own, as a link to a hosted
  // report would. That tab loads the session and finds the report on its stream.
  function openReport(id: string): void {
    window.open(reportUrl(location, sessionId, id), "_blank", "noopener");
  }
  function closeReport(): void {
    if (openedHere) return history.back();
    // Opened from its link: there is no studio page behind it to go back to.
    history.pushState(null, "", `${location.pathname}${location.search}`);
    report = null;
  }

  async function create(prompt: string): Promise<void> {
    try {
      const created = await startSession<GovernedAnalyticsAgent>(transport, { workflowType: "GovernedAnalyticsAgent" });
      setTitle(created.workflow_id, prompt);
      startError = null;
      open(created.workflow_id, prompt);
      void refreshSessions();
    } catch {
      startError = `Can't create a session at ${API}. Is \`just dev\` running?`;
    }
  }
</script>

<div class="app">
  <Sidebar {sessions} current={sessionId} {titles} {apiDown} onselect={(id) => open(id)} onnew={() => open("")} />
  {#key sessionId}
    <Studio
      sessionId={sessionId || null}
      {transport}
      title={sessionId ? (titles[sessionId] ?? "Untitled analysis") : "New analysis"}
      temporalUi={TEMPORAL_UI}
      {initialPrompt}
      oncreate={create}
      ontitle={(text) => sessionId && !titles[sessionId] && setTitle(sessionId, text)}
      {report}
      onopenreport={openReport}
      onclosereport={closeReport}
    />
  {/key}
  {#if startError}<div class="toast">{startError}</div>{/if}
</div>

<style>
  .app { height: 100vh; display: grid; grid-template-columns: 248px minmax(0, 1fr) 360px; }
  .toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); padding: 10px 14px; border-radius: 9px; background: var(--panel-3); border: 1px solid color-mix(in srgb, var(--bad) 50%, transparent); color: var(--bad); box-shadow: var(--shadow); font-size: 12.5px; }
  @media (max-width: 1180px) {
    .app { grid-template-columns: 210px minmax(0, 1fr) 320px; }
  }
  @media (max-width: 900px) {
    :global(body) { overflow: auto; }
    .app { height: auto; grid-template-columns: 1fr; }
  }
</style>
