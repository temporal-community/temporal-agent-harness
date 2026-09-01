<script lang="ts">
  /**
   * Which session you are in, and everything done to a session as a whole.
   *
   * It was three objects on this row — a "+ New" menu, a "Sessions" menu, and a
   * status chip whose detail line repeated the agent's name the chat pane header
   * had already stated. They are one object now, because they are one question,
   * and because the row could not afford them.
   *
   * The strip carries a labeled New chip (word only, no +) next to the session
   * anchor. The pane launcher’s icon-only + stays at the trail — that glyph opens
   * a pane; this chip starts a session. The popover is either the sessions list
   * or the agent picker, depending on which strip control opened it — no duplicate
   * New control inside.
   */
  import { ChevronDown, RefreshCw, Search, X } from "@lucide/svelte";
  import type { AgentDescriptor, Session } from "$lib/api/types";
  import AgentGlyph from "$lib/components/primitives/AgentGlyph.svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import StatusChip, {
    STATUS_TONES,
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";
  import { dismissable } from "$lib/state/dismissable.svelte";

  type MenuTab = "sessions" | "new";

  interface Props {
    sessions?: Session[];
    agents?: AgentDescriptor[];
    sessionId: string;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    refreshingSessions?: boolean;
    showSessionPicker?: boolean;
    closed?: boolean;
    closedWorkflowIds?: string[];
    error?: string | null;
    /** List refresh failure — shown in the popover, not as stream "Needs attention". */
    sessionsError?: string | null;
    awaitingRegistration?: boolean;
    pendingApprovalCount?: number;
    onNewSession?: (workflowType: string) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
    onRefreshSessions?: () => void | Promise<void>;
    /** Quiet enrich when the picker opens (age-gated). */
    onEnsureSessions?: () => void | Promise<void>;
  }

  let {
    sessions = [],
    agents = [],
    sessionId,
    connecting = false,
    sending = false,
    creatingSession = false,
    refreshingSessions = false,
    showSessionPicker = true,
    closed = false,
    closedWorkflowIds = [],
    error = null,
    sessionsError = null,
    awaitingRegistration = false,
    pendingApprovalCount = 0,
    onNewSession,
    onSelectSession,
    onRefreshSessions,
    onEnsureSessions
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

  let menuOpen = $state(false);
  let menuTab = $state<MenuTab>("sessions");
  let sessionSearch = $state("");
  let searchElement = $state<HTMLInputElement | undefined>();
  let agentListElement = $state<HTMLElement | undefined>();

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
              : awaitingRegistration
                ? "Waiting for agent"
              : "Available"
  );
  const agentTitle = $derived(
    activeAgent?.label ?? activeSession?.agent_workflow_type ?? "No session"
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

  function sessionInitialMessage(session: Session): string {
    return (
      session.initial_user_message?.trim() ||
      (session.is_spawned ? session.label : "No user message yet")
    );
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
    if (awaitingRegistration) return "idle";
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

  /**
   * Session anchor: open/close sessions mode. If the New picker is already open,
   * switch to sessions instead of closing — mirrors the New chip’s toggle.
   */
  function toggleMenu(): void {
    if (menuOpen && menuTab === "new") {
      menuTab = "sessions";
      void onEnsureSessions?.();
      return;
    }
    if (menuOpen) {
      closeMenu();
      return;
    }
    menuTab = "sessions";
    menuOpen = true;
    void onEnsureSessions?.();
  }

  /**
   * Whether the keyboard is put back on the anchor is not decided here.
   *
   * A flag on each caller would be: Escape wants the anchor back, a press on something
   * else wants to be left alone, and every call site has to get that right. It is the
   * same rule for every layer in the app, so `dismissable` states it once — focus
   * returns only if the layer still had it — and no caller carries the decision.
   */
  function closeMenu(): void {
    if (!menuOpen) return;
    menuOpen = false;
    /* Closing ends the errand, so the next press is the same press as the last
       one: the list of sessions, which is what the anchor is asked for. */
    menuTab = "sessions";
  }

  /** Strip New chip: open (or toggle shut) the popover already on the new mode. */
  function openNewSessionMenu(): void {
    if (!canCreateSession) return;
    if (menuOpen && menuTab === "new") {
      closeMenu();
      return;
    }
    menuTab = "new";
    menuOpen = true;
  }

  /* Opening a picker puts focus on what answers the question — search for
     sessions, first agent row for new. */
  $effect(() => {
    if (!menuOpen) return;
    if (menuTab === "sessions") searchElement?.focus();
    else agentListElement?.querySelector<HTMLElement>(".agent-row")?.focus();
  });

  async function startNewSession(workflowType: string): Promise<void> {
    if (!workflowType || !onNewSession || creatingSession) return;
    await onNewSession(workflowType);
    closeMenu();
  }

  async function openSession(nextSessionId: string): Promise<void> {
    if (!onSelectSession) return;
    if (nextSessionId !== sessionId) {
      await onSelectSession(nextSessionId);
    }
    closeMenu();
  }

  async function refreshSessions(): Promise<void> {
    if (!onRefreshSessions || refreshingSessions) return;
    await onRefreshSessions();
  }
</script>

<div class="session-controls">
  {#if onRefreshSessions}
    <IconButton
      class={refreshingSessions ? "session-refresh spinning" : "session-refresh"}
      label="Refresh all sessions"
      data-tip-below
      data-tip-align="start"
      disabled={refreshingSessions}
      onclick={() => void refreshSessions()}
    >
      <RefreshCw size={14} />
    </IconButton>
  {/if}

  <!-- The pip is the Chip's own, tinted by the status tone, so the mark that
       says how the run is doing cannot drift from the spoken label beside it.

       Both chips open one dialog into two views, so each says `expanded` for the
       view it opens rather than for the popover being up at all. A bare `menuOpen`
       on the anchor had it announce itself expanded while rendering collapsed, and
       a reader who pressed Enter to dismiss got the view swapped instead. -->
  {#if showSessionPicker}
    <Chip
      class="session-anchor"
      pip
      tone={statusTone}
      fill="quiet"
      toned
      active={menuOpen && menuTab === "sessions"}
      aria-haspopup="dialog"
      aria-expanded={menuOpen && menuTab === "sessions"}
      aria-label={`${agentTitle} — ${statusLabel}. Switch session`}
      data-tip={`${statusLabel} — switch session`}
      data-tip-align="start"
      data-tip-below
      onclick={toggleMenu}
    >
      <span class="session-name">{agentTitle}</span>
      {#if spokenStatus}
        <span class="session-state">{spokenStatus}</span>
      {/if}
      <span class="control-chevron" aria-hidden="true">
        <ChevronDown size={13} />
      </span>
    </Chip>
  {/if}

  <Chip
    class="session-new"
    label={creatingSession ? "Starting" : "New"}
    fill="quiet"
    active={menuOpen && menuTab === "new"}
    aria-haspopup="dialog"
    aria-expanded={menuOpen && menuTab === "new"}
    aria-disabled={canCreateSession ? undefined : "true"}
    data-tip={canCreateSession ? "New session" : "No agents are registered"}
    data-tip-align="start"
    data-tip-below
    onclick={openNewSessionMenu}
  />

  {#if menuOpen}
    <!-- `keep` is the whole control, so the anchor's own toggle is not fought over: a press
         on it would otherwise dismiss here and reopen there, in one press. -->
    <section
      class="session-popover"
      aria-label="Session menu"
      {@attach dismissable({ ondismiss: closeMenu, keep: ".session-controls" })}
    >
      <!-- One row: mode kicker on the left, refresh + close on the right. Status
           already lives on the session anchor (pip / spoken label). -->
      <header class="session-popover-head">
        <span class="kicker" id="session-menu-heading">
          {#if menuTab === "new"}
            New session
          {:else}
            Sessions {sessionItems.length}
          {/if}
        </span>

        <div class="session-popover-actions">
          {#if onRefreshSessions}
            <IconButton
              class={refreshingSessions ? "session-refresh spinning" : "session-refresh"}
              label="Refresh sessions"
              data-tip-below
              data-tip-align="end"
              disabled={refreshingSessions}
              onclick={() => void refreshSessions()}
            >
              <RefreshCw size={14} />
            </IconButton>
          {/if}
          <IconButton
            label="Close session menu"
            data-tip-below
            data-tip-align="end"
            onclick={() => closeMenu()}
          >
            <X size={15} />
          </IconButton>
        </div>
      </header>

      <div class="session-panel" aria-labelledby="session-menu-heading">
        {#if menuTab === "new"}
          <div class="agent-list" role="menu" bind:this={agentListElement}>
            <!-- Keyed on `key`, which load_agent_registry refuses to let repeat.
                 `workflow_type` it does not check, and a Svelte duplicate key throws
                 in production too — so keying on it would turn a survivable typo in
                 agents.toml into an uncaught throw while rendering this list. -->
            {#each agents as agent (agent.key)}
              <button
                type="button"
                class="agent-row"
                role="menuitem"
                onclick={() => void startNewSession(agent.workflow_type)}
              >
                <AgentGlyph
                  label={agent.label}
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
        {:else}
          <label class="session-search">
            <Search size={14} aria-hidden="true" />
            <input
              bind:this={searchElement}
              bind:value={sessionSearch}
              placeholder="Search sessions"
              aria-label="Search sessions"
            />
          </label>

          <div class="session-list">
            {#if sessionsError}
              <p class="session-empty">{sessionsError}</p>
            {/if}
            {#if filteredSessionItems.length === 0}
              <p class="session-empty">No matching sessions.</p>
            {/if}
            {#each filteredSessionItems as item (item.workflow_id)}
              <button
                type="button"
                class={`session-row ${item.workflow_id === sessionId ? "active" : ""}`}
                aria-current={item.workflow_id === sessionId ? "true" : undefined}
                onclick={() => void openSession(item.workflow_id)}
              >
                <AgentGlyph
                  label={sessionAgentLabel(item)}
                  status={glyphStatusForSession(item)}
                />
                <span class="session-copy">
                  <time>{sessionCreatedAt(item.created_at)}</time>
                  <strong>{sessionInitialMessage(item)}</strong>
                  <small>{sessionAgentLabel(item)}{item.is_spawned ? " · spawned" : ""}</small>
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
        {/if}
      </div>
    </section>
  {/if}
</div>

<style>
  /* `0 1 auto`, not `none`: this sits in the minimap's lead zone, and the mark next
     to it is centred in the window rather than between its neighbours, so a lead
     that refuses to shrink does not get pushed — it grows over the mark. Shrinking
     is what makes the anchor's `max-width: min(100%, 38vw)` bite, which is what
     turns a long session name into an ellipsis instead of a collision. */
  .session-controls {
    position: relative;
    flex: 0 1 auto;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
  }

  /* aria-disabled keeps the tip readable when no agents are registered. */
  :global(.session-new[aria-disabled="true"]) {
    opacity: var(--disabled-opacity);
    cursor: default;
  }

  /* The anchor is a Chip, so its box, its tone and its press are the app's. Width
     follows the session name; one existing ceiling so a pathological name cannot
     push the map off the row. 38vw was already the viewport half of this cap. */
  :global(.session-anchor) {
    width: auto;
    max-width: min(100%, 38vw);
  }

  .session-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Second in the same chip rather than a chip of its own: one object saying one
     thing about one session. */
  .session-state {
    flex: none;
    padding-left: 6px;
    border-left: 1px solid var(--border);
    color: var(--text-2);
  }

  .control-chevron {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--text-3);
    transition: transform var(--duration-fast) var(--ease-ui), color var(--duration-fast) var(--ease-ui);
  }

  /* The chip owns the class, so reaching its open and hover states from here has
     to cross the component boundary. The hue is the chip's own --chip-color, so
     the chevron cannot drift from the control it sits in. */
  :global(.session-anchor.active) .control-chevron {
    color: color-mix(in srgb, var(--chip-color) 78%, var(--text-1));
    transform: rotate(180deg);
  }

  :global(.session-anchor:focus-visible) .control-chevron {
    color: color-mix(in srgb, var(--chip-color) 78%, var(--text-1));
  }

  @media (hover: hover) and (pointer: fine) {
    :global(.session-anchor:hover) .control-chevron {
      color: color-mix(in srgb, var(--chip-color) 78%, var(--text-1));
    }
  }

  /* Grows out of the anchor it came from, which is at the left end of the status
     line, so it is pinned and scaled from that corner. The chrome popovers sit in
     the 40s so they clear every pane-level overlay, which tops out at 30. */
  .session-popover {
    position: absolute;
    top: calc(100% + var(--gap-sm));
    left: 0;
    z-index: 44;
    width: min(420px, calc(100vw - 32px));
    /* Bounded because the page itself does not scroll: a menu taller than the
       viewport would put its last rows somewhere no gesture can reach. */
    max-height: min(560px, calc(100vh - 104px));
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: var(--gutter-tight);
    padding: var(--gutter);
    border: 1px solid var(--border-strong);
    background: var(--surface-1);
    box-shadow: var(--shadow-popover);
    transform-origin: top left;
    animation: session-popover-in var(--duration-fast) var(--ease-out);
  }

  @keyframes session-popover-in {
    from {
      opacity: 0;
      transform: scale(0.97) translateY(-3px);
    }
  }

  .session-popover-head {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  /* Actions hold the right edge via margin-left: auto. */
  .session-popover-actions {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
  }

  /* Both header buttons are IconButtons, so the box, the hover, the focus ring
     and the disabled dimming are the app's. The one thing left here is the spin,
     which is why IconButton takes a `class` that merges instead of one that
     replaces. `:global` because the class rides across a component boundary and
     so carries none of this file's scoping. */
  :global(.session-refresh.spinning svg) {
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

  .session-panel {
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--gutter-tight);
  }

  .session-search {
    min-width: 0;
    height: var(--control-height-lg);
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 8px;
    align-items: center;
    padding: 0 10px;
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
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
    font-size: var(--font-md);
  }

  .session-search input::placeholder {
    color: var(--text-3);
  }

  .agent-list {
    min-height: 0;
    overflow-y: auto;
    display: grid;
    align-content: start;
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
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--surface-2) 42%, var(--surface-1));
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
    transition:
      border-color var(--duration-fast) var(--ease-ui),
      background var(--duration-fast) var(--ease-ui),
      transform var(--duration-fast) var(--ease-ui);
  }

  /* Inward, unlike the baseline ring in app.css: the lists scroll, so an outline
     drawn outside a full-width row is clipped away by the container and only the
     top edge of it survives. */
  .agent-row:focus-visible,
  .session-row:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  @media (hover: hover) and (pointer: fine) {
    .agent-row:hover {
      border-color: color-mix(in srgb, var(--accent) 42%, var(--border-strong));
      background: color-mix(in srgb, var(--accent) 7%, var(--surface-2));
      transform: translateY(-1px);
    }
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
    font-size: var(--font-md);
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-copy small {
    min-width: 0;
    overflow: hidden;
    color: var(--text-3);
    font-size: var(--font-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-list {
    flex: 1;
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
    border-radius: var(--radius-md);
    background: color-mix(in srgb, var(--surface-2) 42%, var(--surface-1));
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
    transition:
      border-color var(--duration-fast) var(--ease-ui),
      background var(--duration-fast) var(--ease-ui),
      transform var(--duration-fast) var(--ease-ui);
  }

  @media (hover: hover) and (pointer: fine) {
    .session-row:hover {
      border-color: color-mix(in srgb, var(--reasoning) 38%, var(--border-strong));
      background: color-mix(in srgb, var(--reasoning) 5%, var(--surface-2));
      transform: translateY(-1px);
    }
  }

  .session-row.active {
    border-color: color-mix(in srgb, var(--accent) 54%, var(--border));
    background: color-mix(in srgb, var(--accent) 14%, var(--surface-1));
  }

  .session-copy {
    min-width: 0;
    display: grid;
    gap: 3px;
  }

  .session-copy time {
    color: var(--text-3);
    font-size: var(--font-sm);
  }

  .session-copy strong {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: var(--font-md);
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-copy small {
    min-width: 0;
    overflow: hidden;
    color: var(--text-3);
    font-size: var(--font-sm);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .session-empty {
    margin: 6px 0;
    color: var(--text-3);
    font-size: var(--font-md);
  }

  @media (prefers-reduced-motion: reduce) {
    /* Still reads as busy, by dimming rather than by spinning. */
    :global(.session-refresh.spinning svg) {
      animation: none;
      opacity: 0.6;
    }

    .session-popover {
      animation: none;
    }

    .agent-row:hover,
    .session-row:hover {
      transform: none;
    }

    .control-chevron,
    .agent-row,
    .session-row {
      transition: none;
    }
  }
</style>
