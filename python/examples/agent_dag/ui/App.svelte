<!-- Agent DAG Studio: the session picker, and the studio for the chosen flow. Each session is one
     DagBuilderAgent; "New flow" opens a draft that becomes a session on its first prompt. -->
<script lang="ts">
  import { HttpTransport, startSession, type SessionSummary } from "@temporalio/agent-harness-svelte";

  import type { DagBuilderAgent } from "../client_sdk/DagBuilderAgent";
  import Sidebar from "./Sidebar.svelte";
  import Studio from "./Studio.svelte";

  const params = new URLSearchParams(location.search);
  const API = params.get("api") ?? "http://localhost:8000/api/";
  const TEMPORAL_UI = params.get("temporal-ui") ?? "http://localhost:8233/namespaces/default/workflows";
  const transport = new HttpTransport({ baseUrl: API });

  let sessionId = $state(params.get("session") ?? "");
  let initialPrompt = $state<string | null>(null);
  let sessions = $state<SessionSummary[]>([]);
  let apiDown = $state(false);
  let startError = $state<string | null>(null);

  // Titles are the first prompt of each flow, kept in this browser.
  let titles = $state<Record<string, string>>(loadTitles());
  function loadTitles(): Record<string, string> {
    try {
      return JSON.parse(localStorage.getItem("agent-dag:titles") ?? "{}") as Record<string, string>;
    } catch {
      return {};
    }
  }
  function setTitle(id: string, title: string): void {
    titles = { ...titles, [id]: title.length > 60 ? `${title.slice(0, 57)}…` : title };
    try {
      localStorage.setItem("agent-dag:titles", JSON.stringify(titles));
    } catch {
      // A private window: titles just won't survive a reload.
    }
  }

  async function refreshSessions(): Promise<void> {
    try {
      const all = await transport.listSessions();
      sessions = all.filter((s) => s.agent_workflow_type === "DagBuilderAgent").sort((a, b) => b.created_at - a.created_at);
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
    history.pushState(null, "", id ? `?session=${encodeURIComponent(id)}` : location.pathname);
  }
  $effect(() => {
    const onPop = () => open(new URLSearchParams(location.search).get("session") ?? "");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  });

  async function create(prompt: string): Promise<void> {
    try {
      const created = await startSession<DagBuilderAgent>(transport, { workflowType: "DagBuilderAgent" });
      setTitle(created.workflow_id, prompt);
      startError = null;
      open(created.workflow_id, prompt);
      void refreshSessions();
    } catch {
      startError = `Can't create a session at ${API}. Are \`just server\`, \`just session-manager\` and \`just worker\` running?`;
    }
  }
</script>

<div class="app">
  <Sidebar {sessions} current={sessionId} {titles} {apiDown} onselect={(id) => open(id)} onnew={() => open("")} />
  {#key sessionId}
    <Studio
      sessionId={sessionId || null}
      {transport}
      title={sessionId ? (titles[sessionId] ?? "Untitled flow") : "New flow"}
      temporalUi={TEMPORAL_UI}
      {initialPrompt}
      oncreate={create}
      ontitle={(text) => sessionId && !titles[sessionId] && setTitle(sessionId, text)}
    />
  {/key}
  {#if startError}<div class="toast">{startError}</div>{/if}
</div>

<style>
  .app { height: 100vh; display: grid; grid-template-columns: 248px minmax(0, 1fr) 390px; }
  .toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); padding: 10px 14px; border-radius: 9px; background: var(--panel-3); border: 1px solid color-mix(in srgb, var(--bad) 50%, transparent); color: var(--bad); box-shadow: var(--shadow); font-size: 12.5px; }
  @media (max-width: 1180px) {
    .app { grid-template-columns: 210px minmax(0, 1fr) 330px; }
  }
  @media (max-width: 900px) {
    :global(body) { overflow: auto; }
    .app { height: auto; grid-template-columns: 1fr; }
  }
</style>
