<script lang="ts">
  /** The launcher identifies the current run; the drawer only browses existing sessions. */
  import { Plus, RefreshCw, Search } from "@lucide/svelte";
  import type { Attachment } from "svelte/attachments";
  import type { AgentDescriptor, JsonRecord, Session } from "$lib/api/types";
  import StartSessionDialog from "$lib/components/chat/StartSessionDialog.svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import StatusChip, {
    STATUS_TONES,
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";
  import { scrollFollower } from "$lib/state/followScroll";
  import { handleListKey, keptHighlight } from "$lib/state/quickSwitch";
  import { sessionDay, sessionTitle } from "$lib/state/sessionHistory";

  type Display = "launcher" | "pane";

  interface Props {
    sessions?: Session[];
    agents?: AgentDescriptor[];
    sessionId: string;
    display?: Display;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    refreshingSessions?: boolean;
    closed?: boolean;
    closedWorkflowIds?: string[];
    error?: string | null;
    /** List refresh failure — shown in the manager, not as stream "Needs attention". */
    sessionsError?: string | null;
    /** What the agent is waiting on the reader for, e.g. "1 response needed". */
    pendingLabel?: string | null;
    /** `data` is the agent's init data, for an agent that takes it (see `init_data`). */
    onNewSession?: (workflowType: string, data?: JsonRecord) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
    onRefreshSessions?: () => void | Promise<void>;
    /** Re-list when setup opens, so availability reflects the current workers. */
    onEnsureAgents?: () => void | Promise<void>;
    /** Re-check availability without changing the selected agent or entered data. */
    onRefreshAgents?: () => void | Promise<void>;
    refreshingAgents?: boolean;
    /** Agent re-list failure — shown in the picker, like `sessionsError`. */
    agentsError?: string | null;
    /** Pane only: Enter and Escape from the keyboard are done with the drawer. */
    onClose?: () => void;
  }

  let {
    sessions = [],
    agents = [],
    sessionId,
    display = "launcher",
    connecting = false,
    sending = false,
    creatingSession = false,
    refreshingSessions = false,
    closed = false,
    closedWorkflowIds = [],
    error = null,
    sessionsError = null,
    pendingLabel = null,
    onNewSession,
    onSelectSession,
    onRefreshSessions,
    onEnsureAgents,
    onRefreshAgents,
    refreshingAgents = false,
    agentsError = null,
    onClose
  }: Props = $props();

  /**
   * The states that get words on the anchor as well as a hue.
   *
   * Everything else is the pip alone, and the reason is churn: connecting and
   * thinking turn over several times a turn, and the pane minimap is laid out
   * immediately after this control in the same flex row, so a label that grows
   * and shrinks here would slide the tick a reader is aiming at. These states do
   * not churn — they last until a person acts — so they are the ones worth a
   * word beside the pip on the anchor.
   */
  const SPOKEN_KINDS = new Set<StatusKind>([
    "approval",
    "error",
    "blocked",
    "stuck",
    "closed"
  ]);

  let newSessionOpen = $state(false);
  let agentFilter = $state("");
  let sessionSearch = $state("");

  const focusFirst: Attachment<HTMLElement> = (node) => {
    const frame = requestAnimationFrame(() => {
      node.focus();
    });
    return () => cancelAnimationFrame(frame);
  };

  const sessionItems = $derived(sortedSessions(sessions));
  const sessionSearchTerm = $derived(sessionSearch.trim().toLowerCase());
  const agentOptions = $derived(
    [...new Set(sessionItems.map((session) => session.agent_workflow_type))].map((type) => ({
      type, label: agents.find((agent) => agent.workflow_type === type)?.label || type
    })).sort((a, b) => a.label.localeCompare(b.label))
  );
  const filteredSessionItems = $derived(sessionItems.filter((session) =>
    (!agentFilter || session.agent_workflow_type === agentFilter) &&
    (!sessionSearchTerm || sessionMatchesSearch(session, sessionSearchTerm))
  ));
  const sessionGroups = $derived.by(() => {
    const groups: { label: string; sessions: Session[] }[] = [];
    for (const session of filteredSessionItems) {
      const label = sessionDay(session.created_at);
      const last = groups.at(-1);
      if (last?.label === label) last.sessions.push(session);
      else groups.push({ label, sessions: [session] });
    }
    return groups;
  });
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
        : pendingLabel
          ? pendingLabel
          : sending
            ? "Thinking"
            : error
              ? "Needs attention"
              : "Available"
  );
  const agentTitle = $derived(
    activeAgent?.label || activeSession?.agent_workflow_type || "No session"
  );
  const statusTone = $derived(STATUS_TONES[statusKind]);
  const spokenStatus = $derived(SPOKEN_KINDS.has(statusKind) ? statusLabel : null);

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

  function sessionTime(value: number): string {
    return value ? new Date(value * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  }

  function sessionAgentLabel(session: Session): string {
    return (
      agents.find((agent) => agent.workflow_type === session.agent_workflow_type)?.label ||
      session.agent_workflow_type
    );
  }

  function currentStatusKind(): StatusKind {
    if (closed) return "closed";
    if (error) return "error";
    if (pendingLabel) return "approval";
    if (creatingSession) return "starting";
    if (connecting) return "connecting";
    if (sending) return "thinking";
    return "available";
  }

  function sessionStatusKind(session: Session): StatusKind {
    if (sessionClosedById(session.workflow_id)) return "closed";
    if (session.workflow_id === sessionId) return statusKind;
    return "idle";
  }

  function sessionStatusLabel(session: Session): string {
    if (sessionClosedById(session.workflow_id)) return "Closed";
    if (session.workflow_id === sessionId) return statusLabel;
    return "Open";
  }

  function sessionClosedById(nextSessionId: string): boolean {
    return (
      (nextSessionId === sessionId && closed) ||
      closedWorkflowIds.includes(nextSessionId) ||
      Boolean(sessions.find((session) => session.workflow_id === nextSessionId)?.closed)
    );
  }

  function sessionMatchesSearch(session: Session, term: string): boolean {
    return [
      sessionTitle(session),
      session.label || "",
      sessionAgentLabel(session),
      session.workflow_id,
      session.agent_workflow_type
    ].some((value) => value.toLowerCase().includes(term));
  }

  function openNewSessionMenu(): void {
    newSessionOpen = true;
    void onEnsureAgents?.();
  }

  async function openSession(nextSessionId: string): Promise<void> {
    if (!onSelectSession) return;
    if (nextSessionId !== sessionId) {
      await onSelectSession(nextSessionId);
    }
  }

  /* What the reader last arrowed to. The row shown as highlighted is derived from it, so a
     filter that hides that row lands on the first match without an effect to keep in step. */
  let arrowedId = $state<string | null>(null);
  const filteredIds = $derived(filteredSessionItems.map((session) => session.workflow_id));
  const highlightedId = $derived(keptHighlight(filteredIds, arrowedId));
  let sessionListElement = $state<HTMLElement | null>(null);
  /* No `onscroll` handed over, on purpose: that is how the follower stands down for a reader
     who scrolled the playhead away, and here every arrow press is the reader asking to see
     the row. */
  const listFollower = scrollFollower(() => sessionListElement);

  $effect(() => {
    if (highlightedId) listFollower.to(sessionOptionId(highlightedId));
  });

  function sessionOptionId(workflowId: string): string {
    return `session-option-${workflowId}`;
  }

  function handleSearchKeydown(event: KeyboardEvent): void {
    handleListKey(event, highlightedId == null ? -1 : filteredIds.indexOf(highlightedId), filteredIds.length, {
      move: (index) => (arrowedId = filteredIds[index]),
      pick: (index) => {
        void openSession(filteredIds[index]);
        onClose?.();
      },
      close: () => onClose?.()
    });
  }

</script>

{#if display === "launcher"}
  <div class="session-controls">
    <Chip
      class="session-new"
      label="New session"
      tone="accent"
      aria-haspopup="dialog"
      onclick={openNewSessionMenu}
    >
      {#snippet lead()}<Plus size={13} aria-hidden="true" />{/snippet}
    </Chip>
    {#snippet identity()}
      <Chip
        class="session-anchor"
        pip
        tone={statusTone}
        fill="bare"
        toned
        aria-label={`${agentTitle} — ${statusLabel}${activeSession ? `. ${sessionTitle(activeSession)}` : ""}`}
        title={activeSession ? `${agentTitle} — ${sessionTitle(activeSession)} · ${statusLabel}` : statusLabel}
      >
        <span class="session-identity">
          <span class="session-name">{agentTitle}</span>
          {#if activeSession}<span class="session-title">{sessionTitle(activeSession)}</span>{/if}
        </span>
        {#if spokenStatus}<span class="session-state">{spokenStatus}</span>{/if}
      </Chip>
    {/snippet}
    {@render identity()}
  </div>
  {#if newSessionOpen}
    <StartSessionDialog
      {agents}
      preferredAgentType={activeSession?.agent_workflow_type}
      starting={creatingSession}
      refreshing={refreshingAgents}
      error={agentsError}
      onRefresh={onRefreshAgents}
      onStart={onNewSession ?? (() => {})}
      onClose={() => (newSessionOpen = false)}
    />
  {/if}
{:else}
  <section class="session-manager" aria-label="Session Manager">
    <header class="session-manager-head">
      <h2>Sessions <span>{sessionItems.length}</span></h2>
      {#if onRefreshSessions}
        <IconButton
          class={refreshingSessions ? "session-refresh spinning" : "session-refresh"}
          label="Refresh sessions"
          data-tip-below
          data-tip-align="end"
          disabled={refreshingSessions}
          onclick={() => void onRefreshSessions?.()}
        ><RefreshCw size={14} /></IconButton>
      {/if}
    </header>
    <label class="session-search">
      <Search size={14} aria-hidden="true" />
      <input
        {@attach focusFirst}
        bind:value={sessionSearch}
        placeholder="Search message, agent, or ID"
        aria-label="Search sessions"
        role="combobox"
        aria-autocomplete="list"
        aria-controls="session-listbox"
        aria-expanded="true"
        aria-activedescendant={highlightedId ? sessionOptionId(highlightedId) : undefined}
        oninput={() => (arrowedId = null)}
        onkeydown={handleSearchKeydown}
      />
    </label>
    {#if agentOptions.length > 1}
      <label class="agent-filter">
        Agent
        <select bind:value={agentFilter} aria-label="Filter by agent" onchange={() => (arrowedId = null)}>
          <option value="">All agents</option>
          {#each agentOptions as option (option.type)}<option value={option.type}>{option.label}</option>{/each}
        </select>
      </label>
    {/if}
    {#if sessionsError}
      <div class="session-error" role="alert">
        <p>{sessionsError}</p>
        {#if onRefreshSessions}
          <Chip size="xs" fill="quiet" disabled={refreshingSessions} onclick={() => void onRefreshSessions?.()}>Retry</Chip>
        {/if}
      </div>
    {/if}
    <div class="session-list" id="session-listbox" role="listbox" aria-label="Sessions" bind:this={sessionListElement}>
      {#if !sessionsError && filteredSessionItems.length === 0}
        <div class="session-empty">
          <strong>{sessionItems.length ? "No matching sessions" : "No sessions yet"}</strong>
          <p>{sessionItems.length ? "Try another search or agent filter." : "Choose New session in the top bar to begin."}</p>
        </div>
      {/if}
      {#each sessionGroups as group (group.label)}
        <div class="session-group" role="group" aria-label={group.label}>
          <div class="group-heading" aria-hidden="true">{group.label} <span>{group.sessions.length}</span></div>
          {#each group.sessions as item (item.workflow_id)}
            <div class="session-item">
              <button
                type="button"
                id={sessionOptionId(item.workflow_id)}
                class={["session-row", item.workflow_id === sessionId && "active", arrowedId !== null && item.workflow_id === highlightedId && "highlighted"]}
                role="option"
                aria-selected={item.workflow_id === highlightedId}
                aria-current={item.workflow_id === sessionId ? "true" : undefined}
                onclick={() => void openSession(item.workflow_id)}
              >
                <span class="session-row-meta">
                  <strong class="session-agent" title={sessionAgentLabel(item)}>{sessionAgentLabel(item)}</strong>
                  <time title={sessionCreatedAt(item.created_at)} datetime={item.created_at ? new Date(item.created_at * 1000).toISOString() : undefined}>{sessionTime(item.created_at)}</time>
                </span>
                <span class="session-preview" title={sessionTitle(item)}>{sessionTitle(item)}</span>
                <span class="session-row-foot">
                  <StatusChip label={sessionStatusLabel(item)} kind={sessionStatusKind(item)} compact />
                </span>
              </button>
            </div>
          {/each}
        </div>
      {/each}
    </div>
  </section>
{/if}

<style>
  .session-controls {
    flex: 0 1 auto;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
  }

  :global(.session-new) {
    flex: none;
  }

  :global(.session-anchor) {
    width: auto;
    max-width: min(100%, 38vw);
    padding-inline: 0;
  }

  .session-identity {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .session-name, .session-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-title {
    color: var(--text-3);
    font-family: var(--font-sans);
    font-size: var(--font-2xs);
    text-transform: none;
    letter-spacing: normal;
  }

  .session-state {
    flex: 0 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    padding-left: var(--gap-sm);
    border-left: 1px solid var(--border);
    color: var(--text-2);
  }

  .session-manager {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gutter-tight);
    padding: var(--gutter);
    background: var(--surface-1);
  }

  .session-manager-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--gap-sm);
  }

  h2 {
    margin: 0;
    font-size: var(--font-lg);
    font-weight: 600;
  }

  h2 span {
    margin-left: var(--gap-sm);
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 400;
  }

  .session-search {
    flex: none;
    height: var(--control-height-lg);
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: var(--gap-sm);
    align-items: center;
    padding-inline: 10px;
    border: 1px solid var(--border);
    background: var(--control-bg);
    color: var(--text-3);
  }

  .session-search:focus-within {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--focus-ring);
  }

  .session-search input {
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    color: var(--text-1);
    font: inherit;
    font-size: var(--font-sm);
  }

  .session-search input::placeholder {
    color: var(--text-3);
  }

  .agent-filter {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .agent-filter select {
    flex: 1;
    min-width: 0;
    height: var(--control-height);
    padding-inline: var(--gap-sm);
    border: 1px solid var(--border);
    background: var(--control-bg);
    color: var(--text-2);
    font: inherit;
  }

  .session-list {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }

  .session-group {
    display: grid;
    gap: var(--gap-xs);
  }

  .group-heading {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    margin: 0;
    padding: var(--gutter) 2px var(--gap-sm);
    font-size: var(--font-sm);
    font-weight: 500;
    color: var(--text-2);
  }

  .group-heading span {
    color: var(--text-3);
    font-size: var(--font-2xs);
    font-weight: 400;
  }

  .session-item {
    position: relative;
    min-width: 0;
    display: grid;
  }

  .session-row {
    min-width: 0;
    display: grid;
    gap: var(--gap-sm);
    padding: var(--gutter-tight);
    border: 1px solid transparent;
    border-left: 2px solid transparent;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
    transition: background var(--duration-fast) var(--ease-ui), border-color var(--duration-fast) var(--ease-ui);
  }

  .session-row:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  .session-row.highlighted {
    background: var(--control-hover);
  }

  .session-row.active {
    border-left-color: var(--accent);
    background: color-mix(in srgb, var(--accent) 7%, var(--surface-1));
  }

  .session-row-meta {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: var(--gap-sm);
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .session-agent {
    font-size: var(--font-lg);
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-1);
  }

  time {
    flex: none;
    font-variant-numeric: tabular-nums;
  }

  .session-preview {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
    overflow-wrap: anywhere;
    color: var(--text-3);
    font-size: var(--font-sm);
    line-height: 1.5;
    font-weight: 400;
  }

  .session-row-foot {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--gap-sm);
    min-width: 0;
  }

  .session-empty {
    padding-block: var(--gutter);
    color: var(--text-2);
    font-size: var(--font-sm);
    line-height: 1.5;
  }

  .session-empty p {
    color: var(--text-3);
  }

  .session-error {
    display: flex;
    gap: var(--gap-sm);
    align-items: baseline;
    justify-content: space-between;
    color: var(--error);
    font-size: var(--font-sm);
  }

  .session-error p {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
  }

  :global(.session-refresh.spinning svg) {
    animation: session-refresh-spin 800ms linear infinite;
  }

  @keyframes session-refresh-spin {
    to {
      transform: rotate(360deg);
    }

  }

  @media (hover: hover) and (pointer: fine) {
    .session-item:hover .session-row {
      background: var(--control-hover);
    }

  }

  @media (prefers-reduced-motion: reduce) {
    :global(.session-refresh.spinning svg) {
      animation: none;
      opacity: 0.6;
    }

    .session-row {
      transition: none;
    }

  }
</style>
