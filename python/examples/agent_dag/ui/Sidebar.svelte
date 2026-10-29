<!-- Every DagBuilderAgent session the server knows about, newest first. -->
<script lang="ts">
  import type { SessionSummary } from "@temporalio/agent-harness-svelte";

  interface Props {
    sessions: readonly SessionSummary[];
    current: string;
    titles: Readonly<Record<string, string>>;
    apiDown: boolean;
    onselect: (id: string) => void;
    onnew: () => void;
  }
  let { sessions, current, titles, apiDown, onselect, onnew }: Props = $props();

  let filter = $state("");

  const titleOf = (s: SessionSummary) => titles[s.workflow_id] ?? "Untitled flow";
  const shown = $derived(
    sessions.filter((s) => !filter.trim() || titleOf(s).toLowerCase().includes(filter.trim().toLowerCase()))
  );

  function ago(seconds: number): string {
    const delta = Math.max(0, Date.now() / 1000 - seconds);
    if (delta < 60) return "just now";
    if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
    return new Date(seconds * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
</script>

<aside>
  <div class="brand">
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <rect x="1" y="1" width="30" height="30" rx="8" />
      <circle cx="9" cy="11" r="3" /><circle cx="9" cy="21" r="3" /><circle cx="23" cy="16" r="3.4" />
      <path d="M12 11c4 0 5 5 7.6 5M12 21c4 0 5-5 7.6-5" />
    </svg>
    <div>
      <b>Agent DAG Studio</b>
      <small>flows of agents, in code</small>
    </div>
  </div>

  <button class="new" onclick={onnew} class:active={!current}>
    <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" /></svg>
    New flow
  </button>

  <div class="search">
    <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5 14 14" /></svg>
    <input bind:value={filter} placeholder="Search flows" aria-label="Search flows" />
  </div>

  <h3>Sessions <span>{sessions.length}</span></h3>
  <nav>
    {#if apiDown}
      <p class="note bad">Can't reach the harness API. Are <code>just server</code> and <code>just worker</code> running?</p>
    {:else if shown.length === 0}
      <p class="note">{sessions.length ? "No flows match." : "Your flows will appear here."}</p>
    {/if}
    {#each shown as s (s.workflow_id)}
      <button class="session" class:active={s.workflow_id === current} onclick={() => onselect(s.workflow_id)}>
        <span class="dot" class:closed={s.closed}></span>
        <span class="title">{titleOf(s)}</span>
        <span class="when">{s.closed ? "closed" : ago(s.created_at)}</span>
      </button>
    {/each}
  </nav>

  <footer>
    <span>Temporal · agent harness</span>
  </footer>
</aside>

<style>
  aside {
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: var(--panel);
    border-right: 1px solid var(--line);
    padding: 14px 10px 10px;
    gap: 10px;
  }
  .brand { display: flex; align-items: center; gap: 10px; padding: 2px 6px 8px; }
  .brand svg { width: 32px; height: 32px; flex: none; }
  .brand rect { fill: var(--accent-soft); stroke: color-mix(in srgb, var(--accent) 45%, transparent); }
  .brand circle { fill: var(--accent); }
  .brand path { fill: none; stroke: var(--accent); stroke-width: 1.6; opacity: 0.7; }
  .brand b { display: block; font-size: 14px; letter-spacing: -0.01em; }
  .brand small { color: var(--faint); font-size: 11.5px; }

  .new {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    height: 34px;
    border-radius: 8px;
    border: 1px solid color-mix(in srgb, var(--accent) 50%, transparent);
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 600;
    transition: background 0.15s;
  }
  .new:hover { background: color-mix(in srgb, var(--accent) 22%, transparent); }
  .new svg { width: 14px; height: 14px; stroke: currentColor; stroke-width: 2; stroke-linecap: round; }

  .search { position: relative; }
  .search svg { position: absolute; left: 9px; top: 9px; width: 13px; height: 13px; fill: none; stroke: var(--faint); stroke-width: 1.6; }
  .search input {
    width: 100%;
    height: 31px;
    padding: 0 10px 0 29px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--panel-2);
    outline: none;
  }
  .search input:focus { border-color: var(--line-2); }

  h3 {
    margin: 6px 6px 0;
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--faint);
    display: flex;
    justify-content: space-between;
  }
  nav { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 2px; min-height: 0; }
  .session {
    display: grid;
    grid-template-columns: 8px 1fr auto;
    align-items: center;
    gap: 9px;
    padding: 8px 8px;
    border: 0;
    border-radius: 7px;
    background: transparent;
    text-align: left;
    color: var(--muted);
  }
  .session:hover { background: var(--panel-2); color: var(--ink); }
  .session.active { background: var(--panel-3); color: var(--ink); box-shadow: inset 2px 0 0 var(--accent); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--good); box-shadow: 0 0 0 3px color-mix(in srgb, var(--good) 18%, transparent); }
  .dot.closed { background: var(--faint); box-shadow: none; }
  .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
  .when { font-size: 11px; color: var(--faint); }
  .note { margin: 4px 8px; color: var(--faint); font-size: 12px; }
  .note.bad { color: var(--bad); }
  footer { padding: 6px 8px 0; border-top: 1px solid var(--line); color: var(--faint); font-size: 11px; }
</style>
