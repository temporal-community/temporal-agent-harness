<script lang="ts">
  import { ChevronDown, History, Plus, RefreshCw, Search, X } from "@lucide/svelte";
  import type { AgentDescriptor, Session } from "$lib/api/types";
  import AgentGlyph from "$lib/components/primitives/AgentGlyph.svelte";
  import StatusChip, {
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";

  interface Props {
    sessions?: Session[];
    agents?: AgentDescriptor[];
    sessionId: string;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    refreshingSessions?: boolean;
    closed?: boolean;
    closedWorkflowIds?: string[];
    error?: string | null;
    pendingApprovalCount?: number;
    onNewSession?: (workflowType: string) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
    onRefreshSessions?: () => void | Promise<void>;
  }

  let {
    sessions = [],
    agents = [],
    sessionId,
    connecting = false,
    sending = false,
    creatingSession = false,
    refreshingSessions = false,
    closed = false,
    closedWorkflowIds = [],
    error = null,
    pendingApprovalCount = 0,
    onNewSession,
    onSelectSession,
    onRefreshSessions
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
  const activeSession = $derived(
    sessionItems.find((session) => session.workflow_id === sessionId) ?? null
  );
  const activeAgent = $derived(
    agents.find((agent) => agent.workflow_type === activeSession?.agent_workflow_type) ??
      null
  );
  const statusKind = $derived(currentStatusKind());
  const statusLabel = $derived(
    closed
      ? "Closed"
      : creatingSession
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
  const statusDetail = $derived(
    closed
      ? "stopped"
      : error
      ? "intervention"
      : pendingApprovalCount > 0
        ? "human gate"
        : connecting
          ? "stream"
          : sending
            ? "turn active"
            : activeAgent?.label
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

  function currentStatusKind(): StatusKind {
    if (closed) return "closed";
    if (error) return "error";
    if (pendingApprovalCount > 0) return "approval";
    if (creatingSession) return "starting";
    if (connecting) return "connecting";
    if (sending) return "thinking";
    return "available";
  }

  function sessionStatusKind(session: Session): StatusKind {
    if (sessionClosedById(session.workflow_id)) return "closed";
    if (session.workflow_id === sessionId) return statusKind;
    return session.is_message_queuing_enabled ? "queued" : "idle";
  }

  function sessionStatusLabel(session: Session): string {
    if (sessionClosedById(session.workflow_id)) return "Closed";
    if (session.workflow_id === sessionId) return "Active";
    return session.is_message_queuing_enabled ? "Queue on" : "Idle";
  }

  function sessionClosedById(nextSessionId: string): boolean {
    return (
      (nextSessionId === sessionId && closed) ||
      closedWorkflowIds.includes(nextSessionId) ||
      Boolean(sessions.find((session) => session.workflow_id === nextSessionId)?.closed)
    );
  }

  function glyphStatusForSession(
    session: Session
  ): "available" | "busy" | "approval" | "error" | "idle" {
    if (sessionClosedById(session.workflow_id)) return "idle";
    if (session.workflow_id !== sessionId) return "idle";
    if (statusKind === "error") return "error";
    if (statusKind === "approval") return "approval";
    if (statusKind === "available" || statusKind === "complete") return "available";
    return "busy";
  }

  function agentDescription(agent: AgentDescriptor): string {
    return agent.description?.trim() || agent.workflow_type;
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

  async function refreshSessions(): Promise<void> {
    if (!onRefreshSessions || refreshingSessions) return;
    await onRefreshSessions();
  }
</script>

<div class="session-controls">
  <div class="new-session-anchor">
    <button
      type="button"
      class="session-add"
      class:disabled={!canCreateSession}
      class:active={newSessionMenuOpen}
      disabled={!canCreateSession}
      aria-haspopup="menu"
      aria-expanded={newSessionMenuOpen}
      onclick={toggleNewSessionMenu}
    >
      <Plus size={13} aria-hidden="true" />
      <span>{creatingSession ? "Starting" : "New"}</span>
      <span class="control-chevron" aria-hidden="true">
        <ChevronDown size={13} />
      </span>
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
              <AgentGlyph
                label={agent.label}
                workflowType={agent.workflow_type}
                status="available"
              />
              <span class="agent-copy">
                <strong>{agent.label}</strong>
                <small>{agentDescription(agent)}</small>
              </span>
              <StatusChip label="Ready" kind="available" compact />
            </button>
          {/each}
        </div>
      </section>
    {/if}
  </div>

  <button
    type="button"
    class="session-drawer-button"
    class:active={sessionDrawerOpen}
    aria-pressed={sessionDrawerOpen}
    onclick={toggleSessionPopover}
  >
    <History size={13} />
    <span>Sessions</span>
    <span class="control-chevron" aria-hidden="true">
      <ChevronDown size={13} />
    </span>
  </button>

  <StatusChip
    label={statusLabel}
    kind={statusKind}
    detail={statusDetail}
    active={statusKind === "thinking" || statusKind === "connecting"}
  />

  {#if sessionDrawerOpen}
    <section class="session-popover" aria-label="Sessions">
      <header class="session-popover-head">
        <span class="session-popover-title">
          <History size={15} />
          <span>Sessions</span>
        </span>
        <div class="session-popover-actions">
          {#if onRefreshSessions}
            <button
              type="button"
              class="session-popover-refresh"
              class:spinning={refreshingSessions}
              aria-label="Refresh sessions"
              disabled={refreshingSessions}
              onclick={() => void refreshSessions()}
            >
              <RefreshCw size={14} />
            </button>
          {/if}
          <button
            type="button"
            class="session-popover-close"
            aria-label="Close sessions"
            onclick={() => (sessionDrawerOpen = false)}
          >
            <X size={15} />
          </button>
        </div>
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
            <AgentGlyph
              label={sessionAgentLabel(item)}
              workflowType={item.agent_workflow_type}
              status={glyphStatusForSession(item)}
            />
            <span class="session-copy">
              <time>{sessionCreatedAt(item.created_at)}</time>
              <strong>{sessionInitialMessage(item)}</strong>
              <small>{sessionAgentLabel(item)}{item.is_discovered ? " · discovered" : ""}</small>
            </span>
            <StatusChip
              label={sessionStatusLabel(item)}
              kind={sessionStatusKind(item)}
              compact
              active={item.workflow_id === sessionId && statusKind !== "available" && statusKind !== "complete" && statusKind !== "closed"}
            />
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
    --control-accent: var(--accent);
    position: relative;
    min-width: 0;
    height: 32px;
    display: inline-grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 7px;
    padding: 0 10px;
    border: 1px solid color-mix(in srgb, var(--control-accent) 18%, var(--border));
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-2);
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    font-weight: 650;
    box-shadow: inset 0 1px 0 rgb(255 255 255 / 0.04);
    transition:
      border-color 140ms ease,
      background 140ms ease,
      color 140ms ease,
      box-shadow 140ms ease;
  }

  .session-add {
    flex: 0 0 auto;
  }

  .session-drawer-button {
    --control-accent: var(--reasoning);
  }

  .session-add:hover:not(.disabled),
  .session-add:focus-visible:not(.disabled),
  .session-add.active,
  .session-drawer-button:hover,
  .session-drawer-button:focus-visible,
  .session-drawer-button.active {
    border-color: color-mix(in srgb, var(--control-accent) 46%, var(--border-strong));
    color: var(--text-1);
    background: color-mix(in srgb, var(--control-accent) 10%, var(--control-hover));
    box-shadow:
      inset 0 1px 0 rgb(255 255 255 / 0.06),
      0 0 0 3px color-mix(in srgb, var(--control-accent) 16%, transparent);
    outline: 0;
  }

  .control-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--text-3);
    transition: transform 140ms ease, color 140ms ease;
  }

  .session-add.active .control-chevron,
  .session-drawer-button.active .control-chevron {
    color: color-mix(in srgb, var(--control-accent) 78%, white);
    transform: rotate(180deg);
  }

  .session-add:hover:not(.disabled) .control-chevron,
  .session-add:focus-visible:not(.disabled) .control-chevron,
  .session-drawer-button:hover .control-chevron,
  .session-drawer-button:focus-visible .control-chevron {
    color: color-mix(in srgb, var(--control-accent) 78%, white);
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
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-1);
    box-shadow: var(--shadow-popover);
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
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-1);
    box-shadow: var(--shadow-popover);
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

  .session-popover-actions {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .session-popover-refresh,
  .session-popover-close,
  .new-session-close {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-3);
    cursor: pointer;
  }

  .session-popover-refresh:hover:not(:disabled),
  .session-popover-refresh:focus-visible:not(:disabled),
  .session-popover-close:hover,
  .session-popover-close:focus-visible,
  .new-session-close:hover,
  .new-session-close:focus-visible {
    color: var(--text-1);
    border-color: var(--border-strong);
    outline: 0;
  }

  .session-popover-refresh:disabled {
    cursor: default;
    opacity: 0.6;
  }

  .session-popover-refresh.spinning :global(svg) {
    animation: session-refresh-spin 800ms linear infinite;
  }

  @keyframes session-refresh-spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
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
    border-radius: 6px;
    background: var(--control-bg);
    color: var(--text-3);
  }

  .session-search:focus-within {
    border-color: color-mix(in srgb, var(--accent) 48%, var(--border-strong));
    color: var(--text-2);
    box-shadow: 0 0 0 3px var(--focus-ring);
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
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 9px;
    align-items: center;
    padding: 10px;
    border: 1px solid color-mix(in srgb, var(--accent) 12%, var(--border));
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 42%, var(--surface-1));
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
    transition:
      border-color 140ms ease,
      background 140ms ease,
      transform 140ms ease;
  }

  .agent-row:hover,
  .agent-row:focus-visible {
    border-color: color-mix(in srgb, var(--accent) 42%, var(--border-strong));
    outline: 0;
    background: color-mix(in srgb, var(--accent) 7%, var(--surface-2));
    transform: translateY(-1px);
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
    border: 1px solid color-mix(in srgb, var(--reasoning) 10%, var(--border));
    border-radius: 7px;
    background: color-mix(in srgb, var(--surface-2) 42%, var(--surface-1));
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
    transition:
      border-color 140ms ease,
      background 140ms ease,
      transform 140ms ease;
  }

  .session-row:hover,
  .session-row:focus-visible {
    border-color: color-mix(in srgb, var(--reasoning) 38%, var(--border-strong));
    background: color-mix(in srgb, var(--reasoning) 5%, var(--surface-2));
    transform: translateY(-1px);
    outline: 0;
  }

  .session-row.active {
    border-color: color-mix(in srgb, var(--accent) 54%, var(--border));
    background: color-mix(in srgb, var(--accent) 10%, var(--surface-1));
    box-shadow: inset 3px 0 0 var(--accent);
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

  .agent-row :global(.status-chip),
  .session-row :global(.status-chip) {
    justify-self: end;
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
