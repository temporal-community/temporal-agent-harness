<script lang="ts">
  import { ChevronDown, History, Plus, Search, X } from "@lucide/svelte";
  import type { AgentDescriptor, Session } from "$lib/api/types";

  interface Props {
    sessions?: Session[];
    agents?: AgentDescriptor[];
    sessionId: string;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    error?: string | null;
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
    onNewSession,
    onSelectSession
  }: Props = $props();

  let selectedSessionId = $state("");
  let sessionDrawerOpen = $state(false);
  let sessionSearch = $state("");

  const sessionItems = $derived(sortedSessions(sessions));
  const sessionSearchTerm = $derived(sessionSearch.trim().toLowerCase());
  const filteredSessionItems = $derived(
    sessionSearchTerm
      ? sessionItems.filter((session) => sessionMatchesSearch(session, sessionSearchTerm))
      : sessionItems
  );
  const activeSession = $derived(
    sessionItems.find((item) => item.workflow_id === sessionId) ?? null
  );
  const activeSessionLabel = $derived(
    activeSession
      ? `${sessionCreatedAt(activeSession.created_at)} - ${sessionInitialMessage(activeSession)}`
      : "No session"
  );
  const canCreateSession = $derived(
    Boolean(onNewSession) && agents.length > 0 && !connecting && !sending && !creatingSession
  );
  const statusLabel = $derived(
    creatingSession
      ? "Starting"
      : connecting
        ? "Connecting"
        : sending
          ? "Thinking"
          : error
            ? "Needs attention"
            : "Available"
  );

  $effect(() => {
    selectedSessionId = sessionId;
  });

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

  async function handleNewSessionAgentChange(event: Event): Promise<void> {
    const select = event.currentTarget as HTMLSelectElement;
    const workflowType = select.value;
    select.value = "";
    if (!workflowType || !onNewSession || connecting || sending || creatingSession) return;
    await onNewSession(workflowType);
    sessionDrawerOpen = false;
  }

  async function handleSessionChange(): Promise<void> {
    if (!selectedSessionId || !onSelectSession || selectedSessionId === sessionId) return;
    await onSelectSession(selectedSessionId);
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
  <label
    class={`session-picker ${
      !onSelectSession || sessionItems.length === 0 || connecting || creatingSession
        ? "disabled"
        : ""
    }`}
  >
    <History size={13} aria-hidden="true" />
    <span>{activeSessionLabel}</span>
    <select
      bind:value={selectedSessionId}
      aria-label="Select session"
      disabled={!onSelectSession || sessionItems.length === 0 || connecting || creatingSession}
      onchange={() => void handleSessionChange()}
    >
      {#each sessionItems as item}
        <option value={item.workflow_id}>
          {sessionCreatedAt(item.created_at)} - {sessionInitialMessage(item)}
        </option>
      {/each}
    </select>
    <ChevronDown size={12} aria-hidden="true" />
  </label>

  <label class={`session-add ${canCreateSession ? "" : "disabled"}`}>
    <Plus size={13} aria-hidden="true" />
    <span>{creatingSession ? "Starting" : "New"}</span>
    <select
      aria-label="Add session"
      disabled={!canCreateSession}
      onchange={(event) => void handleNewSessionAgentChange(event)}
    >
      <option value="">{creatingSession ? "Starting" : "New"}</option>
      {#each agents as agent}
        <option value={agent.workflow_type}>{agent.label}</option>
      {/each}
    </select>
  </label>

  <button
    type="button"
    class={`session-drawer-button ${sessionDrawerOpen ? "active" : ""}`}
    aria-pressed={sessionDrawerOpen}
    onclick={() => (sessionDrawerOpen = !sessionDrawerOpen)}
  >
    <History size={13} />
    <span>Sessions</span>
  </button>

  <div class="agent-state">
    <span class={`live-dot ${error ? "error" : ""}`} aria-hidden="true"></span>
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

      <label class={`session-popover-add ${canCreateSession ? "" : "disabled"}`}>
        <Plus size={14} aria-hidden="true" />
        <span>{creatingSession ? "Starting" : "New session"}</span>
        <select
          aria-label="Add session"
          disabled={!canCreateSession}
          onchange={(event) => void handleNewSessionAgentChange(event)}
        >
          <option value="">{creatingSession ? "Starting" : "New session"}</option>
          {#each agents as agent}
            <option value={agent.workflow_type}>{agent.label}</option>
          {/each}
        </select>
        <ChevronDown size={13} aria-hidden="true" />
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

  .session-picker,
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

  .session-picker {
    flex: 1 1 260px;
    max-width: 420px;
  }

  .session-add {
    flex: 0 0 auto;
  }

  .session-drawer-button {
    grid-template-columns: auto minmax(0, 1fr);
    border-color: color-mix(in srgb, var(--accent) 24%, var(--border));
  }

  .session-picker:hover:not(.disabled),
  .session-picker:focus-within:not(.disabled),
  .session-add:hover:not(.disabled),
  .session-add:focus-within:not(.disabled),
  .session-drawer-button:hover,
  .session-drawer-button:focus-visible,
  .session-drawer-button.active {
    border-color: var(--border-strong);
    color: var(--text-1);
    outline: 0;
  }

  .session-picker span,
  .session-add span,
  .session-drawer-button span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-picker select,
  .session-add select,
  .session-popover-add select {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    opacity: 0;
    outline: 0;
    appearance: none;
  }

  .session-picker select:disabled,
  .session-add select:disabled,
  .session-popover-add select:disabled {
    cursor: default;
  }

  .session-picker option,
  .session-add option,
  .session-popover-add option {
    background: var(--surface-1);
    color: var(--text-1);
  }

  .session-picker.disabled,
  .session-add.disabled {
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

  .session-popover-head {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .session-popover-title {
    min-width: 0;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--text-1);
    font-size: 13px;
    font-weight: 700;
  }

  .session-popover-close {
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
  .session-popover-close:focus-visible {
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

  .session-popover-add {
    position: relative;
    min-width: 0;
    height: 34px;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 8px;
    align-items: center;
    padding: 0 10px;
    border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--border));
    border-radius: 8px;
    background: color-mix(in srgb, var(--accent) 10%, var(--surface-1));
    color: var(--accent);
    cursor: pointer;
    font-size: 12px;
    font-weight: 650;
  }

  .session-popover-add:hover:not(.disabled),
  .session-popover-add:focus-within:not(.disabled) {
    border-color: color-mix(in srgb, var(--accent) 62%, var(--border));
    outline: 0;
  }

  .session-popover-add.disabled {
    cursor: default;
    opacity: 0.52;
  }

  .session-popover-add span {
    min-width: 0;
    overflow: hidden;
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

    .session-picker {
      flex-basis: min(100%, 360px);
      max-width: none;
    }

    .session-popover {
      right: auto;
      left: 0;
    }
  }
</style>
