<!-- Where to connect: which harness server, and which of its tic-tac-toe sessions. Everything
     about the session itself is below, in SessionView. -->
<script lang="ts">
  import SessionView from "./SessionView.svelte";

  const AGENT_TYPE = "TicTacToeAgent";

  interface Session {
    workflow_id: string;
    created_at: number;
    agent_workflow_type: string;
    closed?: boolean;
  }

  const remembered = (key: string): string | null => {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  };
  const remember = (key: string, value: string): void => {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Storage can be unavailable (private windows); the page just forgets.
    }
  };

  let server = $state(remembered("ttt.server") ?? "http://localhost:8000");
  let sessionId = $state(remembered("ttt.session") ?? "");
  let sessions = $state<Session[]>([]);
  let listError = $state<string | null>(null);
  let creating = $state(false);

  const base = $derived(server.trim().replace(/\/+$/, ""));

  async function loadSessions(): Promise<void> {
    try {
      const response = await fetch(`${base}/api/sessions`);
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      const all = (await response.json()) as Session[];
      sessions = all
        .filter((s) => s.agent_workflow_type === AGENT_TYPE && !s.closed)
        .sort((a, b) => b.created_at - a.created_at);
      listError = null;
      if (sessionId && !sessions.some((s) => s.workflow_id === sessionId)) sessionId = "";
    } catch (error) {
      sessions = [];
      listError = `Cannot reach ${base} — is \`just server\` running? (${error instanceof Error ? error.message : error})`;
    }
  }

  async function newSession(): Promise<void> {
    creating = true;
    try {
      const response = await fetch(`${base}/api/sessions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_workflow_type: AGENT_TYPE })
      });
      if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
      const created = (await response.json()) as Session;
      await loadSessions();
      sessionId = created.workflow_id;
    } catch (error) {
      listError = `Could not create a session: ${error instanceof Error ? error.message : error}`;
    } finally {
      creating = false;
    }
  }

  $effect(() => remember("ttt.server", base));
  $effect(() => remember("ttt.session", sessionId));
  $effect(() => {
    void base;
    void loadSessions();
  });
</script>

<div class="app">
  <header class="bar">
    <h1>Tic-Tac-Toe Ledger <small>a board drawn from observable state</small></h1>
    <div class="field">
      <label for="server">server</label>
      <input id="server" class="server" bind:value={server} spellcheck="false" />
    </div>
    <div class="field">
      <label for="session">session</label>
      <select id="session" class="session" bind:value={sessionId}>
        <option value="">— pick or create —</option>
        {#each sessions as s (s.workflow_id)}
          <option value={s.workflow_id}>
            {new Date(s.created_at * 1000).toLocaleString()} · {s.workflow_id.slice(0, 18)}
          </option>
        {/each}
      </select>
    </div>
    <button class="btn" onclick={newSession} disabled={creating}>New session</button>
  </header>

  {#if listError}
    <div class="notice error">{listError}</div>
  {/if}

  {#if sessionId}
    <!-- A new server is a new set of connections; a new session on the same server is not. -->
    {#key base}
      <SessionView {base} {sessionId} />
    {/key}
  {:else}
    <p class="hint">Pick a session to replay its history, or create one.</p>
  {/if}
</div>
