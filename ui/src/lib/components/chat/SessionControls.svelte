<script lang="ts">
  /**
   * Which session you are in, and everything done to a session as a whole.
   *
   * It was three objects on this row — a "+ New" menu, a "Sessions" menu, and a
   * status chip whose detail line repeated the agent's name the chat pane header
   * had already stated. They are one object now, because they are one question,
   * and because the row could not afford them.
   *
   * Two displays, one component. The `launcher` is the strip in the app's
   * top-left — the session name and its status, which open the Session Manager.
   * The `pane` display is the manager itself, which lives in the left drawer,
   * above the panes it selects. New sessions start from the drawer's own tab.
   */
  import { RefreshCw, Search } from "@lucide/svelte";
  import type { Attachment } from "svelte/attachments";
  import type { AgentDescriptor, Session } from "$lib/api/types";
  import AgentGlyph from "$lib/components/primitives/AgentGlyph.svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import StatusChip, {
    STATUS_TONES,
    type StatusKind
  } from "$lib/components/primitives/StatusChip.svelte";

  type MenuTab = "sessions" | "new";
  type Display = "launcher" | "pane";

  interface Props {
    sessions?: Session[];
    agents?: AgentDescriptor[];
    sessionId: string;
    display?: Display;
    tab?: MenuTab;
    /** Launcher only: whether the drawer this opens is on screen. */
    paneOpen?: boolean;
    connecting?: boolean;
    sending?: boolean;
    creatingSession?: boolean;
    refreshingSessions?: boolean;
    closed?: boolean;
    closedWorkflowIds?: string[];
    error?: string | null;
    /** List refresh failure — shown in the manager, not as stream "Needs attention". */
    sessionsError?: string | null;
    pendingApprovalCount?: number;
    onNewSession?: (workflowType: string) => void | Promise<void>;
    onSelectSession?: (sessionId: string) => void | Promise<void>;
    onRefreshSessions?: () => void | Promise<void>;
    /** Quiet enrich when the picker opens (age-gated). */
    onEnsureSessions?: () => void | Promise<void>;
    /** Quiet re-list when the agent picker opens. Not age-gated — see `openNewSessionMenu`. */
    onEnsureAgents?: () => void | Promise<void>;
    /** The refresh control, in agent-picker mode: re-ask which agents have a worker. */
    onRefreshAgents?: () => void | Promise<void>;
    refreshingAgents?: boolean;
    /** Agent re-list failure — shown in the picker, like `sessionsError`. */
    agentsError?: string | null;
    onTabChange?: (tab: MenuTab) => void;
  }

  let {
    sessions = [],
    agents = [],
    sessionId,
    display = "launcher",
    tab: menuTab = "sessions",
    paneOpen = false,
    connecting = false,
    sending = false,
    creatingSession = false,
    refreshingSessions = false,
    closed = false,
    closedWorkflowIds = [],
    error = null,
    sessionsError = null,
    pendingApprovalCount = 0,
    onNewSession,
    onSelectSession,
    onRefreshSessions,
    onEnsureSessions,
    onEnsureAgents,
    onRefreshAgents,
    refreshingAgents = false,
    agentsError = null,
    onTabChange
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

  let sessionSearch = $state("");

  const focusFirst: Attachment<HTMLElement> = (node) => {
    const frame = requestAnimationFrame(() => {
      (node.matches("input") ? node : node.querySelector<HTMLElement>(".agent-row"))?.focus();
    });
    return () => cancelAnimationFrame(frame);
  };

  const sessionItems = $derived(sortedSessions(sessions));
  const sessionSearchTerm = $derived(sessionSearch.trim().toLowerCase());
  const filteredSessionItems = $derived(
    sessionSearchTerm
      ? sessionItems.filter((session) => sessionMatchesSearch(session, sessionSearchTerm))
      : sessionItems
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
  const agentTitle = $derived(
    activeAgent?.label ?? activeSession?.agent_workflow_type ?? "No session"
  );
  /* One control, two errands. The manager's header is shared by both views, so the refresh
     button belongs to whichever list is on screen — refreshing sessions while looking at
     the agent picker was the bug: it spun, and nothing the reader was looking at changed. */
  const refreshingCurrent = $derived(
    menuTab === "new" ? refreshingAgents : refreshingSessions
  );
  const canRefreshCurrent = $derived(
    Boolean(menuTab === "new" ? onRefreshAgents : onRefreshSessions)
  );
  const refreshLabel = $derived(
    menuTab === "new" ? "Re-check for workers" : "Refresh sessions"
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
    return "idle";
  }

  function sessionStatusLabel(session: Session): string {
    if (sessionClosedById(session.workflow_id)) return "Closed";
    if (session.workflow_id === sessionId) return "Active";
    return "Idle";
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

  /**
   * The agent row's readiness chip.
   *
   * This used to be a hardcoded "Ready" on every row, which was a claim about a worker made
   * from the registry alone — the one thing the registry cannot tell you. Clicking an agent
   * whose worker was down produced a session that started and then hung, its workflow tasks
   * sitting on a queue nobody polls.
   *
   * `ready` is stated flatly rather than hedged. Temporal requires every worker on a task
   * queue to register the same workflow types, so a poller on the queue serves every agent
   * declared for it; hedging would be hedging against a configuration Temporal does not
   * support. A server that could not ask says `unknown` — never a quiet "Ready".
   */
  function agentWorkerStatusKind(agent: AgentDescriptor): StatusKind {
    switch (agent.worker?.status) {
      case "ready":
        return "available";
      case "no_worker":
        return "blocked";
      default:
        return "idle";
    }
  }

  function agentWorkerStatusLabel(agent: AgentDescriptor): string {
    switch (agent.worker?.status) {
      case "ready":
        return "Ready";
      case "no_worker":
        return "No worker";
      default:
        return "Unknown";
    }
  }

  /**
   * The line under the description, for the two states that ask something of the reader.
   *
   * It carries the whole message — the condition AND what to do about it — because there is
   * no reliable hover surface here to put the second half on. The app's `data-tip` is
   * painted with the host's own pseudo-elements (app.css: "painted, not portalled"), and
   * this list is a scroll box, so a bubble above the first row is clipped away by
   * `.agent-list` exactly when someone hovers the thing they cannot click. A native `title`
   * survives that but costs a second of still hover to appear, which is too well hidden for
   * the one line that says why the row will not open.
   *
   * So it is always on screen: no hover, no delay, and it reads the same to a keyboard or
   * touch user. `ready` gets nothing — the chip already said it, and the poller count is
   * trivia.
   */
  function agentWorkerNote(
    agent: AgentDescriptor
  ): { lead: string; tail: string } | null {
    switch (agent.worker?.status) {
      case "ready":
        return null;
      case "no_worker":
        return { lead: "No worker polling", tail: " — start one to use this agent" };
      default:
        return { lead: "Could not check for workers on", tail: "" };
    }
  }

  function agentGlyphStatus(agent: AgentDescriptor): "available" | "idle" {
    return agent.worker?.status === "ready" ? "available" : "idle";
  }

  /**
   * Only `no_worker` blocks the row. `unknown` means the server could not ask — refusing on
   * that would turn a failed health check into a locked door, which is a worse lie than the
   * hardcoded "Ready" this all replaced.
   */
  function agentBlocked(agent: AgentDescriptor): boolean {
    return agent.worker?.status === "no_worker";
  }

  /**
   * Why the row will not open, as a native `title`.
   *
   * Not the app's `data-tip`: that bubble is painted with the host's own pseudo-elements
   * (app.css says so in as many words — "painted, not portalled"), and this list is a
   * scrolling list inside a clipped pane. A tip on the first row would
   * be clipped away by `.agent-list` exactly when someone hovers the thing they cannot
   * click. `title` is the browser's, drawn above everything, and cannot be cut off.
   */
  function agentBlockedReason(agent: AgentDescriptor): string | undefined {
    if (!agentBlocked(agent)) return undefined;
    return `Start a worker polling ${agent.task_queue} before opening a session with this agent.`;
  }

  function sessionMatchesSearch(session: Session, term: string): boolean {
    return [
      sessionInitialMessage(session),
      sessionAgentLabel(session),
      session.workflow_id,
      session.agent_workflow_type
    ].some((value) => value.toLowerCase().includes(term));
  }

  function openSessions(): void {
    void onEnsureSessions?.();
    onTabChange?.("sessions");
  }

  /**
   * The drawer's New session tab. Every open re-lists the agents, with no age gate —
   * unlike the sessions side. The rows now carry whether a worker is polling each
   * agent's task queue, and that can change between two opens; a stale "Ready" is
   * what sent people into a session that started and then hung on an unpolled queue.
   */
  function openNewSessionMenu(): void {
    void onEnsureAgents?.();
    onTabChange?.("new");
  }

  /**
   * Takes the agent, not just its type, so the no-worker refusal lives with the press rather
   * than only in the markup — a row that renders inert must also be inert when activated by
   * a keyboard, which `aria-disabled` alone does not arrange.
   */
  async function startNewSession(agent: AgentDescriptor): Promise<void> {
    if (agentBlocked(agent)) return;
    if (!agent.workflow_type || !onNewSession || creatingSession) return;
    await onNewSession(agent.workflow_type);
  }

  async function openSession(nextSessionId: string): Promise<void> {
    if (!onSelectSession) return;
    if (nextSessionId !== sessionId) {
      await onSelectSession(nextSessionId);
    }
  }

  async function refreshCurrent(): Promise<void> {
    if (refreshingCurrent) return;
    if (menuTab === "new") await onRefreshAgents?.();
    else await onRefreshSessions?.();
  }
</script>

{#if display === "launcher"}
  <div class="session-controls">
  <!-- The pip is the Chip's own, tinted by the status tone, so the mark that
       says how the run is doing cannot drift from the spoken label beside it.
       The copy control sits beside the chip, not in it: the chip is a button. -->
  {#snippet anchor()}
    <Chip
      class="session-anchor"
      pip
      tone={statusTone}
      fill="quiet"
      toned
      active={paneOpen && menuTab === "sessions"}
      aria-expanded={paneOpen && menuTab === "sessions"}
      aria-label={`${agentTitle} — ${statusLabel}. Open Session Manager`}
      data-tip={`${statusLabel} — open Session Manager`}
      data-tip-align="start"
      data-tip-below
      onclick={openSessions}
    >
      <span class="session-name">{agentTitle}</span>
      {#if spokenStatus}
        <span class="session-state">{spokenStatus}</span>
      {/if}
    </Chip>
  {/snippet}
  {#if sessionId}
    <Copyable value={sessionId} label="Copy workflow ID" data-tip-below>{@render anchor()}</Copyable>
  {:else}
    {@render anchor()}
  {/if}
  </div>
{:else}
    <section class="session-manager" aria-label="Session Manager">
      <header class="session-manager-head">
        <div class="session-tabs">
          <Chip
            label={`Sessions ${sessionItems.length}`}
            size="xs"
            fill="quiet"
            active={menuTab === "sessions"}
            onclick={openSessions}
          />
          <Chip
            label="New session"
            size="xs"
            fill="quiet"
            active={menuTab === "new"}
            onclick={openNewSessionMenu}
          />
        </div>

        <div class="session-manager-actions">
          {#if canRefreshCurrent}
            <IconButton
              class={refreshingCurrent ? "session-refresh spinning" : "session-refresh"}
              label={refreshLabel}
              data-tip-below
              data-tip-align="end"
              disabled={refreshingCurrent}
              onclick={() => void refreshCurrent()}
            >
              <RefreshCw size={14} />
            </IconButton>
          {/if}
        </div>
      </header>

      <div class="session-panel">
        {#if menuTab === "new"}
          {#if agentsError}
            <p class="session-empty">{agentsError}</p>
          {/if}
          <div class="agent-list" role="menu" {@attach focusFirst}>
            <!-- Keyed on `key`, which load_agent_registry refuses to let repeat.
                 `workflow_type` it does not check, and a Svelte duplicate key throws
                 in production too — so keying on it would turn a survivable typo in
                 agents.toml into an uncaught throw while rendering this list. -->
            {#each agents as agent (agent.key)}
              <!-- `aria-disabled`, never `disabled`: the whole point of the state is the
                   explanation attached to it, and `disabled` drops the row out of the tab
                   order and suppresses its tooltip — deleting that explanation for a
                   keyboard user at precisely the moment it becomes true. Same call
                   IconButton and StepController already make. -->
              <button
                type="button"
                class="agent-row"
                role="menuitem"
                aria-disabled={agentBlocked(agent) ? "true" : undefined}
                title={agentBlockedReason(agent)}
                onclick={() => void startNewSession(agent)}
              >
                <AgentGlyph
                  label={agent.label}
                  status={agentGlyphStatus(agent)}
                />
                <span class="agent-copy">
                  <strong>{agent.label}</strong>
                  <small>{agentDescription(agent)}</small>
                  <!-- Named queue, not just "no worker": the reader's next move is to start
                       that worker, and they need to know which one. -->
                  {#if agentWorkerNote(agent)}
                    {@const note = agentWorkerNote(agent)!}
                    <small
                      class={agent.worker?.status === "no_worker"
                        ? "agent-note warn"
                        : "agent-note"}
                    >
                      {note.lead} <code>{agent.task_queue}</code>{note.tail}
                    </small>
                  {/if}
                </span>
                <StatusChip
                  label={agentWorkerStatusLabel(agent)}
                  kind={agentWorkerStatusKind(agent)}
                  compact
                />
              </button>
            {/each}
          </div>
        {:else}
          <label class="session-search">
            <Search size={14} aria-hidden="true" />
            <input
              {@attach focusFirst}
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
              <!-- The copy control overlays the row rather than sitting in it, because the
                   row is a button; it lands in the empty corner under the status chip. -->
              <div class="session-item">
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
                    <small>{sessionAgentLabel(item)}{item.is_discovered ? " · discovered" : ""}</small>
                    <code class="session-id">{item.workflow_id}</code>
                  </span>
                  <StatusChip
                    label={sessionStatusLabel(item)}
                    kind={sessionStatusKind(item)}
                    compact
                    active={item.workflow_id === sessionId && statusKind !== "available" && statusKind !== "complete" && statusKind !== "closed"}
                  />
                </button>
                <Copyable value={item.workflow_id} label="Copy workflow ID" data-tip-align="end" />
              </div>
            {/each}
          </div>
        {/if}
      </div>
    </section>
{/if}

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

  .session-manager {
    flex: 1;
    width: 100%;
    min-height: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    gap: var(--gutter-tight);
    padding: var(--gutter);
    background: var(--surface-1);
  }

  .session-manager-head {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .session-tabs {
    min-width: 0;
    display: flex;
    gap: var(--gap-xs);
  }

  /* Actions hold the right edge via margin-left: auto. */
  .session-manager-actions {
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
    flex: 1;
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
    flex: 1;
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
    .agent-row:hover:not([aria-disabled="true"]) {
      border-color: color-mix(in srgb, var(--accent) 42%, var(--border-strong));
      background: color-mix(in srgb, var(--accent) 7%, var(--surface-2));
      transform: translateY(-1px);
    }
  }

  /* Dead to the pointer, because the look IS the message: a row that says "no worker" must
     not also say "press me". It keeps its focus ring and its place in the tab order — see
     the button's own comment for why that matters here.

     The app's other inert controls take `opacity: var(--disabled-opacity)` wholesale, and
     this one deliberately does not. Those are icons and one-word labels, where dimming
     costs nothing; here the row's own body text is the explanation for why it is dim, and
     `opacity` composites the whole subtree — a child cannot paint back through its parent's
     alpha, so there is no exempting the one line worth reading. So the inertness is said
     with the frame instead: the invitation (accent border, lifted hover) is withdrawn and
     the glyph and title recede, while the description and the warning stay legible. */
  .agent-row[aria-disabled="true"] {
    border-color: var(--border);
    background: color-mix(in srgb, var(--surface-2) 20%, var(--surface-1));
    cursor: default;
  }

  .agent-row[aria-disabled="true"] :global(.agent-glyph),
  .agent-row[aria-disabled="true"] .agent-copy strong {
    opacity: var(--disabled-opacity);
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

  /* Louder than the description above it when it is a warning, because it is the one line
     on the row that changes what the reader does next. `unknown` stays muted — it reports
     that the server could not ask, which is not the agent's fault and not yet the reader's
     problem. Both keep the ellipsis discipline of their sibling: a long queue name must not
     widen the pane. */
  .agent-copy small.agent-note.warn {
    color: var(--error);
  }

  /* The one line allowed to wrap. Its siblings ellipse because a description is expendable
     past the first clause; this one ends in the instruction, so clipping it would cut off
     the half that says what to do. */
  .agent-copy small.agent-note {
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
  }

  .agent-copy small.agent-note code {
    font-family: var(--font-mono);
    font-size: inherit;
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
      background var(--duration-fast) var(--ease-ui);
  }

  /* The lift is the item's, not the row's, so the overlaid copy control rises with it. */
  .session-item {
    position: relative;
    min-width: 0;
    display: grid;
    transition: transform var(--duration-fast) var(--ease-ui);
  }

  .session-item > :global(.copyable) {
    position: absolute;
    right: 10px;
    bottom: 10px;
  }

  .session-item:hover :global(.copy) {
    opacity: 1;
  }

  @media (hover: hover) and (pointer: fine) {
    .session-item:hover .session-row {
      border-color: color-mix(in srgb, var(--reasoning) 38%, var(--border-strong));
      background: color-mix(in srgb, var(--reasoning) 5%, var(--surface-2));
    }

    .session-item:hover {
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

  .session-copy code.session-id {
    min-width: 0;
    overflow: hidden;
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
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

    .agent-row:hover,
    .session-item:hover {
      transform: none;
    }

    .agent-row,
    .session-row,
    .session-item {
      transition: none;
    }
  }
</style>
