<script lang="ts">
  import { History, Plus, Search, X } from "@lucide/svelte";
  import type { AgentDescriptor, Session } from "$lib/api/types";

  interface Props {
    sessions?: Session[];
    agents?: AgentDescriptor[];
    sessionId: string;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    error?: string | null;
    pendingApprovalCount?: number;
    onNewSession?: (workflowType: string) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
  }

  let {
    sessions = [],
    agents = [],
    sessionId,
    connecting = false,
    sending = false,
    creatingSession = false,
    error = null,
    pendingApprovalCount = 0,
    onNewSession,
    onSelectSession
  }: Props = $props();

  let sessionDrawerOpen = $state(false);
  let newSessionMenuOpen = $state(false);
  let sessionSearch = $state("");

  const sessionItems = $derived(sortedSessions(sessions));
  const sessionSearchTerm = $derived(sessionSearch.trim().toLowerCase());
  const filteredSessionItems = $derived(
    sessionSearchTerm
      ? sessionItems.filter((session) => sessionMatchesSearch(session, sessionSearchTerm))
      : sessionItems
  );
  const canCreateSession = $derived(
    Boolean(onNewSession) && agents.length > 0 && !creatingSession
  );
  const statusLabel = $derived(
    creatingSession
      ? "Starting"
      : connecting
        ? "Connecting"
        : pendingApprovalCount > 0
          ? `${pendingApprovalCount} approval${pendingApprovalCount === 1 ? "" : "s"} needed`
          : sending
            ? "Thinking"
            : error
              ? "Needs attention"
              : "Available"
  );

  function sortedSessions(value: Session[]): Session[] {
    return [...value].sort((a, b) => b.created_at - a.created_at);
  }

  function sessionCreatedAt(value: number): string {
    if (!value) return "Unknown time";
    return new Date(value * 1000).toLocaleString([], {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function sessionInitialMessage(session: Session): string {
    return session.initial_user_message?.trim() || "No user message yet";
  }

  function sessionAgentLabel(session: Session): string {
    return (
      agents.find((agent) => agent.workflow_type === session.agent_workflow_type)?.label ??
      session.agent_workflow_type
    );
  }

  function sessionMatchesSearch(session: Session, term: string): boolean {
    return [
      sessionInitialMessage(session),
      sessionAgentLabel(session),
      session.workflow_id,
      session.agent_workflow_type
    ].some((value) => value.toLowerCase().includes(term));
  }

  function toggleNewSessionMenu(): void {
    if (!canCreateSession) return;
    newSessionMenuOpen = !newSessionMenuOpen;
    if (newSessionMenuOpen) {
      sessionDrawerOpen = false;
    }
  }

  function toggleSessionPopover(): void {
    sessionDrawerOpen = !sessionDrawerOpen;
    if (sessionDrawerOpen) newSessionMenuOpen = false;
  }

  async function startNewSession(workflowType: string): Promise<void> {
    if (!workflowType || !onNewSession || creatingSession) return;
    await onNewSession(workflowType);
    newSessionMenuOpen = false;
    sessionDrawerOpen = false;
  }

  async function openSession(nextSessionId: string): Promise<void> {
    if (!onSelectSession) return;
    if (nextSessionId !== sessionId) {
      await onSelectSession(nextSessionId);
    }
    sessionDrawerOpen = false;
  }
</script>

<div class="session-controls">
  <div class="new-session-anchor">
    <button
      type="button"
      class={`session-add ${canCreateSession ? "" : "disabled"} ${newSessionMenuOpen ? "active" : ""}`}
      disabled={!canCreateSession}
      aria-haspopup="menu"
      aria-expanded={newSessionMenuOpen}
      onclick={toggleNewSessionMenu}
    >
      <Plus size={13} aria-hidden="true" />
      <span>{creatingSession ? "Starting" : "New"}</span>
    </button>

    {#if newSessionMenuOpen}
      <section class="new-session-popover" aria-label="New session">
        <header class="new-session-head">
          <span class="new-session-title">
            <Plus size={15} />
            <span>New session</span>
          </span>
          <button
            type="button"
            class="new-session-close"
            aria-label="Close new session menu"
            onclick={() => (newSessionMenuOpen = false)}
          >
            <X size={15} />
          </button>
        </header>

        <div class="agent-list" role="menu">
          {#each agents as agent}
            <button
              type="button"
              class="agent-row"
              role="menuitem"
              onclick={() => void startNewSession(agent.workflow_type)}
            >
              <span class="agent-dot" aria-hidden="true"></span>
              <span class="agent-copy">
                <strong>{agent.label}</strong>
                <small>{agent.workflow_type}</small>
              </span>
            </button>
          {/each}
        </div>
      </section>
    {/if}
  </div>

  <button
    type="button"
    class={`session-drawer-button ${sessionDrawerOpen ? "active" : ""}`}
    aria-pressed={sessionDrawerOpen}
    onclick={toggleSessionPopover}
  >
    <History size={13} />
    <span>Sessions</span>
  </button>

  <div class="agent-state">
    <span
      class={`live-dot ${error ? "error" : pendingApprovalCount > 0 ? "approval" : ""}`}
      aria-hidden="true"
    ></span>
    <span>{statusLabel}</span>
  </div>

  {#if sessionDrawerOpen}
    <section class="session-popover" aria-label="Sessions">
      <header class="session-popover-head">
        <span class="session-popover-title">
          <History size={15} />
          <span>Sessions</span>
        </span>
        <button
          type="button"
          class="session-popover-close"
          aria-label="Close sessions"
          onclick={() => (sessionDrawerOpen = false)}
        >
          <X size={15} />
        </button>
      </header>

      <label class="session-search">
        <Search size={14} aria-hidden="true" />
        <input
          bind:value={sessionSearch}
          placeholder="Search sessions"
          aria-label="Search sessions"
        />
      </label>

      <div class="session-list">
        {#if filteredSessionItems.length === 0}
          <p class="session-empty">No matching sessions.</p>
        {/if}
        {#each filteredSessionItems as item}
          <button
            type="button"
            class={`session-row ${item.workflow_id === sessionId ? "active" : ""}`}
            aria-current={item.workflow_id === sessionId ? "true" : undefined}
            onclick={() => void openSession(item.workflow_id)}
          >
            <span class="session-dot" aria-hidden="true"></span>
            <span class="session-copy">
              <time>{sessionCreatedAt(item.created_at)}</time>
              <strong>{sessionInitialMessage(item)}</strong>
              <small>{sessionAgentLabel(item)}</small>
            </span>
            {#if item.workflow_id === sessionId}
              <span class="session-current">Active</span>
            {/if}
          </button>
        {/each}
      </div>
    </section>
  {/if}
</div>

<style>
  .session-controls {
    position: relative;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }

  .new-session-anchor {
    position: relative;
    display: inline-flex;
  }

  .session-add,
  .session-drawer-button {
    position: relative;
    min-width: 0;
    height: 32px;
    display: inline-grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 7px;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-0);
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    font-weight: 650;
  }

  .session-add {
    flex: 0 0 auto;
    grid-template-columns: auto minmax(0, 1fr);
  }

  .session-drawer-button {
    grid-template-columns: auto minmax(0, 1fr);
    border-color: color-mix(in srgb, var(--accent) 24%, var(--border));
  }

  .session-add:hover:not(.disabled),
  .session-add:focus-visible:not(.disabled),
  .session-add.active,
  .session-drawer-button:hover,
  .session-drawer-button:focus-visible,
  .session-drawer-button.active {
    border-color: var(--border-strong);
    color: var(--text-1);
    outline: 0;
  }

  .session-add span,
  .session-drawer-button span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-add.disabled,
  .session-add:disabled {
    cursor: default;
    opacity: 0.52;
  }

  .agent-state {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--success);
    font-size: 12px;
    font-weight: 650;
    white-space: nowrap;
  }

  .live-dot {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: var(--success);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--success) 16%, transparent);
  }

  .live-dot.error {
    background: var(--error);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--error) 16%, transparent);
  }

  .live-dot.approval {
    background: var(--queue);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--queue) 18%, transparent);
  }

  .session-popover {
    position: absolute;
    top: calc(100% + 10px);
    right: 0;
    z-index: 20;
    width: min(420px, calc(100vw - 32px));
    max-height: min(560px, calc(100vh - 104px));
    min-height: 0;
    overflow: hidden;
    display: grid;
    grid-template-rows: auto auto auto minmax(0, 1fr);
    gap: 10px;
    padding: 14px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-0);
    box-shadow: 0 18px 42px rgb(0 0 0 / 0.28);
  }

  .new-session-popover {
    position: absolute;
    top: calc(100% + 10px);
    left: 0;
    z-index: 22;
    width: min(360px, calc(100vw - 32px));
    min-height: 0;
    overflow: hidden;
    display: grid;
    gap: 10px;
    padding: 14px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-0);
    box-shadow: 0 18px 42px rgb(0 0 0 / 0.28);
  }

  .session-popover-head,
  .new-session-head {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .session-popover-title,
  .new-session-title {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--text-1);
    font-size: 13px;
    font-weight: 700;
  }

  .session-popover-close,
  .new-session-close {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: var(--surface-1);
    color: var(--text-3);
    cursor: pointer;
  }

  .session-popover-close:hover,
  .session-popover-close:focus-visible,
  .new-session-close:hover,
  .new-session-close:focus-visible {
    color: var(--text-1);
    border-color: var(--border-strong);
    outline: 0;
  }

  .session-search {
    min-width: 0;
    height: 34px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-1);
    color: var(--text-3);
  }

  .session-search:focus-within {
    border-color: var(--border-strong);
    color: var(--text-2);
  }

  .session-search input {
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    color: var(--text-1);
    font: inherit;
    font-size: 12px;
  }

  .session-search input::placeholder {
    color: var(--text-3);
  }

  .agent-list {
    min-height: 0;
    display: grid;
    gap: 8px;
  }

  .agent-row {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 9px;
    align-items: start;
    padding: 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-1);
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .agent-row:hover,
  .agent-row:focus-visible {
    border-color: var(--border-strong);
    outline: 0;
    background: color-mix(in srgb, var(--surface-2) 38%, var(--surface-0));
  }

  .agent-dot {
    width: 8px;
    height: 8px;
    margin-top: 5px;
    border-radius: 999px;
    background: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
  }

  .agent-copy {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .agent-copy strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: 12px;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-copy small {
    min-width: 0;
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-list {
    min-height: 0;
    overflow-y: auto;
    display: grid;
    align-content: start;
    gap: 8px;
  }

  .session-row {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 9px;
    align-items: start;
    padding: 10px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface-1);
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .session-row:hover,
  .session-row:focus-visible {
    border-color: var(--border-strong);
    outline: 0;
  }

  .session-row.active {
    border-color: color-mix(in srgb, var(--accent) 46%, var(--border));
    background: color-mix(in srgb, var(--accent) 8%, var(--surface-1));
  }

  .session-current {
    align-self: start;
    padding: 3px 6px;
    border: 1px solid color-mix(in srgb, var(--accent) 36%, var(--border));
    border-radius: 999px;
    color: var(--accent);
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
  }

  .session-dot {
    width: 8px;
    height: 8px;
    margin-top: 5px;
    border-radius: 999px;
    background: var(--text-3);
  }

  .session-row.active .session-dot {
    background: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%, transparent);
  }

  .session-copy {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .session-copy time {
    color: var(--text-3);
    font-size: 11px;
  }

  .session-copy strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: 12px;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-copy small {
    min-width: 0;
    overflow: hidden;
    color: var(--text-3);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-empty {
    margin: 6px 0;
    color: var(--text-3);
    font-size: 12px;
  }

  @media (max-width: 980px) {
    .session-controls {
      justify-content: flex-start;
    }

    .session-popover {
      right: auto;
      left: 0;
    }
  }
</style>
