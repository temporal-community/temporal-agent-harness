<script lang="ts">
  /**
   * Which session you are in, and everything done to a session as a whole.
   *
   * It was three objects on this row — a "+ New" menu, a "Sessions" menu, and a
   * status chip whose detail line repeated the agent's name the chat pane header
   * had already stated. They are one object now, because they are one question,
   * and because the row could not afford them: the launcher at the far end of
   * this same strip is also a 13px Plus, so the two would have been the same
   * glyph twice on one 40px row meaning different things.
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

  /**
   * The states that get words on the anchor as well as a hue.
   *
   * Everything else is the pip alone, and the reason is churn: connecting and
   * thinking turn over several times a turn, and the pane minimap is laid out
   * immediately after this control in the same flex row, so a label that grows
   * and shrinks here would slide the tick a reader is aiming at. These states do
   * not churn — they last until a person acts — so they are the ones worth a
   * word. The full sentence is in the menu, on a chip with room for it.
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
              : "Available"
  );
  /* The agent's name is not a detail here any more: the anchor this menu hangs
     off states it, so repeating it under the status chip would be the third
     reading of it on screen after the chat pane's own header. */
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
            : null
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

  function toggleMenu(): void {
    menuOpen = !menuOpen;
  }

  /**
   * Whether the keyboard is put back on the anchor is not decided here.
   *
   * It used to be, by a `restoreFocus` flag every caller had to get right: Escape wanted
   * the anchor back, a press on something else wanted to be left alone. That is the same
   * rule for every layer in the app, and `dismissable` states it once — focus returns only
   * if the layer still had it — so the flag and the two callers that had to pass it go.
   */
  function closeMenu(): void {
    if (!menuOpen) return;
    menuOpen = false;
    /* Closing ends the errand, so the next press is the same press as the last
       one: the list of sessions, which is what the anchor is asked for. */
    menuTab = "sessions";
  }

  function selectTab(tab: MenuTab): void {
    /* The guard `disabled` would have given for free. It is `aria-disabled`
       instead, because a disabled button takes no pointer events and this is
       the one state where its tip is worth reading. */
    if (tab === "new" && !canCreateSession) return;
    menuTab = tab;
  }

  /* Arrows move between the tabs, and the one that is on is the only one in the
     page's tab order — the same walk the pane rail gives its own row. */
  function handleTabKeydown(event: KeyboardEvent): void {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    selectTab(menuTab === "sessions" ? "new" : "sessions");
  }

  /* Opening a picker means asking which session, so the field that answers that
     takes the caret. Runs on a tab change too: the new tab's list is the one the
     keyboard is now in. */
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
  <!-- The pip is the Chip's own, tinted by the status tone, so the mark that
       says how the run is doing cannot drift from the chip that says it in
       words inside the menu. -->
  <Chip
    class="session-anchor"
    pip
    tone={statusTone}
    fill="quiet"
    toned
    active={menuOpen}
    aria-haspopup="dialog"
    aria-expanded={menuOpen}
    aria-label={`${agentTitle} — ${statusLabel}. Switch session or start a new one`}
    data-tip={`${statusLabel} — switch session or start a new one`}
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

  {#if menuOpen}
    <!-- `keep` is the whole control, so the anchor's own toggle is not fought over: a press
         on it would otherwise dismiss here and reopen there, in one press. -->
    <section
      class="session-popover"
      aria-label="Session menu"
      {@attach dismissable({ ondismiss: closeMenu, keep: ".session-controls" })}
    >
      <!-- The status in words, once, where there is room for the whole sentence
           the anchor's pip stands in for. -->
      <header class="session-popover-head">
        <StatusChip
          label={statusLabel}
          kind={statusKind}
          detail={statusDetail}
          active={statusKind === "thinking" || statusKind === "connecting"}
          size="sm"
        />
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

      <!-- The count rides the tab rather than the anchor: on the strip it would
           churn the row's width the way a spoken status would, and at rest it is
           not a number anyone acts on. Here it is the whole of the choice being
           made — pick one of these, or start another. -->
      <div class="session-tabs" role="tablist" aria-label="Sessions or a new one">
        <Chip
          class="session-tab"
          id="session-tab-sessions"
          label={`Sessions ${sessionItems.length}`}
          fill="quiet"
          active={menuTab === "sessions"}
          role="tab"
          aria-selected={menuTab === "sessions"}
          aria-controls="session-menu-panel"
          tabindex={menuTab === "sessions" ? 0 : -1}
          onclick={() => selectTab("sessions")}
          onkeydown={handleTabKeydown}
        />
        <Chip
          class="session-tab"
          id="session-tab-new"
          label={creatingSession ? "Starting" : "New"}
          fill="quiet"
          active={menuTab === "new"}
          aria-disabled={canCreateSession ? undefined : "true"}
          data-tip={canCreateSession ? undefined : "No agents are registered"}
          data-tip-below
          role="tab"
          aria-selected={menuTab === "new"}
          aria-controls="session-menu-panel"
          tabindex={menuTab === "new" ? 0 : -1}
          onclick={() => selectTab("new")}
          onkeydown={handleTabKeydown}
        />
      </div>

      <div
        class="session-panel"
        id="session-menu-panel"
        role="tabpanel"
        aria-labelledby={menuTab === "sessions" ? "session-tab-sessions" : "session-tab-new"}
      >
        {#if menuTab === "new"}
          <div class="agent-list" role="menu" bind:this={agentListElement}>
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
        {/if}
      </div>
    </section>
  {/if}
</div>

<style>
  .session-controls {
    position: relative;
    flex: none;
    min-width: 0;
    display: flex;
    align-items: center;
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
    color: color-mix(in srgb, var(--chip-color) 78%, white);
    transform: rotate(180deg);
  }

  :global(.session-anchor:focus-visible) .control-chevron {
    color: color-mix(in srgb, var(--chip-color) 78%, white);
  }

  @media (hover: hover) and (pointer: fine) {
    :global(.session-anchor:hover) .control-chevron {
      color: color-mix(in srgb, var(--chip-color) 78%, white);
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
    justify-content: space-between;
    gap: 10px;
  }

  .session-popover-actions {
    display: inline-flex;
    align-items: center;
    gap: 6px;
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

  .session-tabs {
    display: flex;
    gap: var(--gap-xs);
  }

  /* `aria-disabled`, not `disabled`, so the tab that says why it cannot be
     pressed is still able to say it. The primitive dims for the native state
     only, so the inert look is owed here. */
  :global(.session-tab[aria-disabled="true"]) {
    opacity: var(--disabled-opacity);
    cursor: default;
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
