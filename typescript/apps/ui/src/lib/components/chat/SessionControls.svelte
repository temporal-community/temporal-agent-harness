<script lang="ts">
  /** The launcher identifies the current run; the drawer only browses existing sessions. */
  import { Archive, ArchiveRestore, ChevronRight, Clock, ExternalLink, Pin, Plus, RefreshCw, Search, X, XCircle } from "@lucide/svelte";
  import { createVirtualizer, type VirtualItem } from "@tanstack/svelte-virtual";
  import { tick, untrack, type Snippet } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import { MediaQuery } from "svelte/reactivity";
  import type { AgentDescriptor, JsonRecord, Session } from "$lib/api/types";
  import EndSessionDialog, { type EndTarget } from "$lib/components/chat/EndSessionDialog.svelte";
  import StartSessionDialog from "$lib/components/chat/StartSessionDialog.svelte";
  import Chip from "$lib/components/primitives/Chip.svelte";
  import IconAgent from "$lib/components/primitives/IconAgent.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import Select from "$lib/components/primitives/Select.svelte";
  import StateGlyph from "$lib/components/primitives/StateGlyph.svelte";
  import { STATUS_TONES, type StatusKind } from "$lib/components/primitives/StatusChip.svelte";
  import TipCard, { type TipField } from "$lib/components/primitives/TipCard.svelte";
  import { windowedFocus } from "$lib/components/primitives/windowedFocus";
  import {
    readArchivedSessions,
    readOperatorPrefs,
    readPinnedSessions,
    toggleArchivedSession,
    togglePinnedSession,
    writeOperatorPrefs
  } from "$lib/state/agentRunStorage";
  import { handleListKey, keptHighlight } from "$lib/state/quickSwitch";
  import { markdownToText } from "$lib/components/chat/MarkdownMessage.svelte";
  import { dissolveClear, inputClearDissolve } from "$lib/motion/inputClearDissolve";
  import {
    DEFAULT_TEMPORAL_UI,
    SESSION_GONE_TIP,
    SESSION_HEADING_HEIGHT,
    SESSION_ROW_HEIGHT,
    UNTITLED_SESSION,
    agentName,
    everyMinute,
    humanizeWorkflowType,
    knownStart,
    lastSeenPending,
    rememberPending,
    sessionAge,
    sessionDay,
    sessionHeadline,
    sessionTitle,
    temporalWorkflowUrl
  } from "$lib/state/sessionHistory";

  type Display = "launcher" | "pane";

  const ARCHIVED = "Archived";

  interface Props {
    sessions?: Session[];
    agents?: AgentDescriptor[];
    sessionId: string;
    display?: Display;
    connecting?: boolean;
    /** When the connection under way started, epoch ms: the anchor counts up from it. */
    connectingSince?: number | null;
    sending?: boolean;
    creatingSession?: boolean;
    refreshingSessions?: boolean;
    closed?: boolean;
    /** Temporal no longer has the open session's workflow; see `SESSION_GONE_TIP`. */
    gone?: boolean;
    closedWorkflowIds?: string[];
    error?: string | null;
    /** List refresh failure — shown in the manager, not as stream "Needs attention". */
    sessionsError?: string | null;
    /** `sessions` came from the server, not just a row painted from cache. */
    sessionsLoaded?: boolean;
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
    /** End any listed session's workflow; rejects with the reason it could not. */
    onEndSession?: (sessionId: string) => Promise<void>;
    /** A session was archived: stop any streaming or polling of it. */
    onArchiveSession?: (sessionId: string) => void;
    /** Bound, so the chat's "Start a new session" opens this same setup dialog. */
    newSessionOpen?: boolean;
  }

  let {
    sessions = [],
    agents = [],
    sessionId,
    display = "launcher",
    connecting = false,
    connectingSince = null,
    sending = false,
    creatingSession = false,
    refreshingSessions = false,
    closed = false,
    gone = false,
    closedWorkflowIds = [],
    error = null,
    sessionsError = null,
    sessionsLoaded = false,
    pendingLabel = null,
    onNewSession,
    onSelectSession,
    onRefreshSessions,
    onEnsureAgents,
    onRefreshAgents,
    refreshingAgents = false,
    agentsError = null,
    onClose,
    onEndSession,
    onArchiveSession,
    newSessionOpen = $bindable(false)
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

  let agentFilter = $state("");
  let sessionSearch = $state("");
  let sessionSearchInput = $state<HTMLInputElement | null>(null);

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
      type, label: agentName(agents, type)
    })).sort((a, b) => a.label.localeCompare(b.label))
  );
  const filteredSessionItems = $derived(sessionItems.filter((session) =>
    (!agentFilter || session.agent_workflow_type === agentFilter) &&
    (!sessionSearchTerm || sessionMatchesSearch(session, sessionSearchTerm))
  ));
  /* One clock for every row's age, ticking on the minute; the day groups read it too, so
     "Today" rolls over while the drawer is open. */
  let now = $state(Date.now());
  $effect(() => (display === "pane" ? everyMinute((ms) => (now = ms)) : undefined));
  /* Most recently pinned first. Pins of sessions not in this list are kept, not pruned. */
  let pins = $state(readPinnedSessions());
  const pinRank = $derived(new Map(pins.map((id, rank) => [id, rank])));
  /* Hidden from the groups, not deleted. An archived session keeps its pin, so unarchiving
     puts it back where it was. Unlisted ids are kept, like pins. */
  let archived = $state(readArchivedSessions());
  const archivedIds = $derived(new Set(archived));
  let archiveOpen = $state(readOperatorPrefs().sessionArchiveOpen === true);
  const archivedItems = $derived(filteredSessionItems.filter((session) => archivedIds.has(session.workflow_id)));
  const sessionGroups = $derived.by(() => {
    const groups: { label: string; sessions: Session[] }[] = [];
    const listed = filteredSessionItems.filter((session) => !archivedIds.has(session.workflow_id));
    /* Stable, so a reused id's runs stay newest first under their one pin. */
    const pinned = listed
      .filter((session) => pinRank.has(session.workflow_id))
      .sort((a, b) => pinRank.get(a.workflow_id)! - pinRank.get(b.workflow_id)!);
    if (pinned.length) groups.push({ label: "Pinned", sessions: pinned });
    const today = new Date(now);
    for (const session of listed) {
      if (pinRank.has(session.workflow_id)) continue;
      const label = sessionDay(session.created_at, today);
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
  /* Agents the registry reports no worker for: their open sessions can't move, opened or not. */
  const workerless = $derived(
    new Set(agents.filter((agent) => agent.worker?.status === "no_worker").map((agent) => agent.workflow_type))
  );
  /* So a waiting session's row keeps saying so once the reader leaves it. */
  $effect(() => rememberPending(sessionId, pendingLabel, connecting));
  let clock = $state(Date.now());
  $effect(() => {
    if (connectingSince == null) return;
    clock = Date.now();
    const timer = setInterval(() => (clock = Date.now()), 1000);
    return () => clearInterval(timer);
  });
  const headline = $derived(
    sessionHeadline({
      closed,
      gone,
      failed: activeSession ? sessionFailed(activeSession) : false,
      noWorker: activeSession ? workerless.has(activeSession.agent_workflow_type) : false,
      error: Boolean(error),
      pendingLabel,
      creating: creatingSession,
      connecting,
      sending,
      connectingFor: connectingSince == null ? null : (clock - connectingSince) / 1000,
      taskQueue: activeAgent?.task_queue
    })
  );
  const statusKind = $derived(headline.kind);
  const statusLabel = $derived(headline.label);
  /* A name only when this session is actually in the list. Until then the status is the
     whole line — "No session" beside "Connecting" was two labels for one missing run. The
     quiet empty desk still says there is no session. */
  const agentTitle = $derived(
    activeSession
      ? agentName(agents, activeSession.agent_workflow_type)
      : statusKind === "available"
        ? "No session"
        : statusLabel
  );
  const agentDescription = $derived(
    markdownToText(activeAgent?.description).trim().replace(/\s+/g, " ")
  );
  /* Title and agent, when there is no worker sentence. That sentence is its own card:
     putting it in this string too made the text bubble announce it a second time. */
  const identityTip = $derived(
    activeSession
      ? `${sessionTitle(activeSession)}\n${agentDescription ? `${agentTitle} — ${agentDescription}` : agentTitle}`
      : statusLabel
  );
  const identityName = $derived(
    activeSession
      ? `${agentTitle} — ${sessionTitle(activeSession)} · ${statusLabel}`
      : agentTitle === statusLabel
        ? statusLabel
        : `${agentTitle} · ${statusLabel}`
  );
  const statusTone = $derived(STATUS_TONES[statusKind]);
  /* Beside a real name only. When the title already is the status, a second copy of it
     is the same clutter as "No session" next to "Connecting". */
  const spokenStatus = $derived(activeSession && SPOKEN_KINDS.has(statusKind) ? statusLabel : null);

  const countFormat = new Intl.NumberFormat();

  function sortedSessions(value: Session[]): Session[] {
    return [...value].sort((a, b) => b.created_at - a.created_at);
  }

  function sessionStarted(value: number, ago: string): string {
    if (!knownStart(value)) return "";
    const date = new Date(value * 1000);
    const at = date.toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" });
    return ago ? `${ago} · ${at}` : at;
  }

  /* The row cuts its title and agent short, so the card has both whole; an icon says what each
     line is, so no line spends its width on a label. Status leads, the way the row's glyph
     does; a closed workflow says how it ended. No id: an unbroken one wrapped the card. */
  function sessionTipFields(session: Session, ago: string, glyph: Snippet): TipField[] {
    const name = sessionAgentLabel(session);
    const type = session.agent_workflow_type;
    const seen = lastSeen(session);
    const status = sessionGone(session)
      ? SESSION_GONE_TIP
      : seen
        ? `${seen.label} · as of ${sessionAge(seen.at / 1000, now).long}`
        : sessionStatusWords(session);
    return [
      { key: "Status", icon: glyph, value: status },
      { key: "Agent", icon: agentIcon, value: type && name !== type && name !== humanizeWorkflowType(type) ? `${name} · ${type}` : name },
      { key: "Started", icon: startedIcon, value: sessionStarted(session.created_at, ago) }
    ];
  }

  /* The glyph's shape says the state on its own; these only tint it. */
  const ROW_GLYPH_KINDS = new Set<StatusKind>(["approval", "stuck", "blocked", "error"]);

  function glyphColor(kind: StatusKind): string {
    if (kind === "closed") return "var(--text-4)";
    const tone = STATUS_TONES[kind];
    return tone === "neutral" ? "var(--text-3)" : `var(--${tone})`;
  }

  /* Its row repeats it when the list is one agent's sessions; the name still says it. */
  const showAgent = $derived(!agentFilter && agentOptions.length > 1);

  function sessionAgentLabel(session: Session): string {
    return agentName(agents, session.agent_workflow_type);
  }

  /* FAILED, TERMINATED, and TIMED_OUT are the statuses Temporal reports for a workflow that
     stopped on its own, not because a person closed it — and `execution_status` carries that
     for every row, not only the one open in chat. It must not collapse into the same quiet,
     hollow pip as an ordinary closed session: that hid a session a reader never opened from
     reading as anything but idle. Reuses "error", the kind an open session's own stream
     failure already renders, instead of adding a second status system. */
  const FAILED_EXECUTION_STATUSES = new Set(["FAILED", "TERMINATED", "TIMED_OUT"]);

  function sessionFailed(session: Session): boolean {
    return Boolean(session.execution_status && FAILED_EXECUTION_STATUSES.has(session.execution_status));
  }

  /* Temporal no longer has it: the dev server restarted, retention expired, or it was deleted.
     The closed tone with its own shape (`statusGlyph`); the tip says why. */
  function sessionGone(session: Session): boolean {
    return session.execution_status === "NOT_FOUND";
  }

  function sessionStatusKind(session: Session): StatusKind {
    if (sessionGone(session)) return "closed";
    if (sessionFailed(session)) return "error";
    if (sessionClosed(session)) return "closed";
    if (session.workflow_id === sessionId) return statusKind;
    if (workerless.has(session.agent_workflow_type)) return "stuck";
    if (lastSeen(session)) return "approval";
    return "idle";
  }

  /* What a session not open in chat was waiting on when last left, unless the list has since
     said it closed or the registry says it has no worker: those are current, this is not. */
  function lastSeen(session: Session): { label: string; at: number } | undefined {
    if (session.workflow_id === sessionId || sessionGone(session) || sessionClosed(session)) return undefined;
    if (workerless.has(session.agent_workflow_type)) return undefined;
    return lastSeenPending.get(session.workflow_id);
  }

  function sessionStatusLabel(session: Session): string {
    if (sessionGone(session)) return "Unavailable";
    if (sessionFailed(session)) return "Needs attention";
    if (sessionClosed(session)) return "Closed";
    if (session.workflow_id === sessionId) return statusLabel;
    if (workerless.has(session.agent_workflow_type)) return "No worker";
    const seen = lastSeen(session);
    if (seen) return `${seen.label} when last seen`;
    return "Open";
  }

  /* The status line of the card and the wide row's status column: a closed workflow says how it ended. */
  function sessionStatusWords(session: Session): string {
    const ended = sessionClosed(session) && !sessionGone(session) ? session.execution_status?.toLowerCase().replaceAll("_", " ") : null;
    return ended ? `${sessionStatusLabel(session)} · ${ended}` : sessionStatusLabel(session);
  }

  const startedFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const startedYearFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" });

  /* The wide row's started column. An older year takes the time's place, so the cell keeps its width. */
  function sessionStartedShort(value: number): string {
    if (!knownStart(value)) return "";
    const date = new Date(value * 1000);
    return (date.getFullYear() === new Date(now).getFullYear() ? startedFormat : startedYearFormat).format(date);
  }

  const closedIds = $derived(new Set(closedWorkflowIds));

  /* Runs per rendered row, so it must not scan the session list. */
  function sessionClosed(session: Session): boolean {
    return (
      (session.workflow_id === sessionId && closed) ||
      closedIds.has(session.workflow_id) ||
      Boolean(session.closed)
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

  /* Click and Enter both land here, so the listbox's selection follows what was opened. */
  async function openSession(nextSessionId: string): Promise<void> {
    if (!onSelectSession) return;
    arrowedId = nextSessionId;
    if (nextSessionId !== sessionId) {
      await onSelectSession(nextSessionId);
    }
  }

  /* What the reader last arrowed to. The row shown as highlighted is derived from it, so a
     filter that hides that row lands on the first match without an effect to keep in step. */
  let arrowedId = $state<string | null>(null);
  /* What is drawn: a shut Archived group draws none of its rows, so arrowing skips them, except
     the session open in chat, which archiving must not lose from sight. A search draws every
     archived match: a shut group hid the only hit. */
  const archivedDrawn = $derived(
    archiveOpen || sessionSearchTerm ? archivedItems : archivedItems.filter((session) => session.workflow_id === sessionId)
  );
  const drawnGroups = $derived(
    archivedDrawn.length ? [...sessionGroups, { label: ARCHIVED, sessions: archivedDrawn }] : sessionGroups
  );
  /* A toggle only while it has something to open or close: a shut group drawing its one row,
     the open session, read as a collapsed chevron over everything it held. While searching,
     its matches are drawn whatever the toggle says. */
  const archiveToggles = $derived(!sessionSearchTerm && (archiveOpen || archivedDrawn.length < archivedItems.length));
  /* In the order drawn, so arrowing from search walks the pins first too. */
  const filteredIds = $derived(drawnGroups.flatMap((group) => group.sessions.map((session) => session.workflow_id)));
  const highlightedId = $derived(keptHighlight(filteredIds, arrowedId));
  let sessionListElement = $state<HTMLElement | null>(null);

  /* Only the rows in view are in the DOM: a list of 10,000 took 5 s to open drawn whole.
     Day headings are rows too, so the list scrolls as one; each option still carries
     aria-setsize/aria-posinset, so a screen reader hears the full count. */
  type ListEntry =
    | { kind: "heading"; key: string; group: string; count: number }
    | { kind: "archive"; key: string; group: string; count: number }
    | { kind: "session"; key: string; group: string; item: Session; ordinal: number };
  const entries = $derived.by(() => {
    const out: ListEntry[] = [];
    let ordinal = 0;
    for (const group of drawnGroups) {
      out.push(
        group.label === ARCHIVED && archiveToggles
          ? { kind: "archive", key: "archive", group: ARCHIVED, count: archivedItems.length }
          : { kind: "heading", key: `day:${group.label}`, group: group.label, count: group.sessions.length }
      );
      for (const item of group.sessions) {
        /* A reused workflow id lists one run per day it started on, and Pinned gathers all of
           them, so the day and the start join the key. */
        out.push({ kind: "session", key: `${group.label}:${item.workflow_id}:${item.created_at}`, group: group.label, item, ordinal: (ordinal += 1) });
      }
    }
    if (!archivedDrawn.length && archivedItems.length) out.push({ kind: "archive", key: "archive", group: ARCHIVED, count: archivedItems.length });
    return out;
  });
  const entryIndex = $derived(
    new Map(entries.flatMap((entry, index) => (entry.kind === "session" ? [[entry.item.workflow_id, index] as const] : [])))
  );
  const coarsePointer = new MediaQuery("pointer: coarse");
  const sessionRows = createVirtualizer<HTMLElement, HTMLElement>({
    /* Seeded here as well as in the effect below, which SSR never runs. */
    count: untrack(() => entries.length),
    /* The virtualizer can ask past a list that just shrank, before its count catches up. */
    getItemKey: (index) => entries[index]?.key ?? index,
    /* The CSS heights below, so measuring a row moves nothing; zoomed text still gets measured. */
    estimateSize: (index) =>
      entries[index]?.kind === "heading" || entries[index]?.kind === "archive"
        ? SESSION_HEADING_HEIGHT
        : coarsePointer.current ? SESSION_ROW_HEIGHT.coarse : SESSION_ROW_HEIGHT.fine,
    getScrollElement: () => sessionListElement,
    /* A screenful before the list is measured, so the first paint (and SSR) has rows. */
    initialRect: { width: 0, height: 800 },
    overscan: 6
  });
  /* Untracked: setOptions publishes the store, which would re-run the effect calling it. */
  const rows = () => untrack(() => $sessionRows);
  const measureRow: Attachment<HTMLElement> = (node) => rows().measureElement(node);
  /* The rows in view, back inside their day's group so it is still announced. */
  const visibleGroups = $derived.by(() => {
    const out: { label: string; rows: VirtualItem[] }[] = [];
    for (const row of $sessionRows.getVirtualItems()) {
      const label = entries[row.index].group;
      const last = out.at(-1);
      if (last?.label === label) last.rows.push(row);
      else out.push({ label, rows: [row] });
    }
    return out;
  });

  /* Pre, so a shrunk list's count lands before the rows render and index past its end. A new
     key function too: a pin moves rows without changing the count, and the virtualizer lays
     rows out again only when one of the two changes (it keeps measured sizes by key). */
  $effect.pre(() => {
    const list = entries;
    sessionListElement;
    rows().setOptions({ count: list.length, getItemKey: (index) => list[index]?.key ?? index });
  });

  /* Every arrow press is the reader asking to see the row, so it always scrolls to it. */
  $effect(() => {
    const index = highlightedId ? entryIndex.get(highlightedId) : undefined;
    if (index != null) rows().scrollToIndex(index, { align: "auto" });
  });

  function sessionOptionId(workflowId: string): string {
    return `session-option-${workflowId}`;
  }

  function handleSearchKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape" && !event.isComposing && sessionSearch) {
      event.preventDefault();
      clearSessionSearch();
      return;
    }
    handleListKey(event, highlightedId == null ? -1 : filteredIds.indexOf(highlightedId), filteredIds.length, {
      move: (index) => (arrowedId = filteredIds[index]),
      pick: (index) => {
        void openSession(filteredIds[index]);
        onClose?.();
      },
      close: () => onClose?.()
    });
  }

  function clearSessionSearch(): void {
    dissolveClear(sessionSearchInput, () => (sessionSearch = ""));
    arrowedId = null;
    sessionSearchInput?.focus({ preventScroll: true });
  }

  /* The row moves group, so its pin is redrawn; focus follows it when that row is drawn. */
  async function togglePin(workflowId: string, button: HTMLElement): Promise<void> {
    const focused = document.activeElement === button;
    pins = togglePinnedSession(workflowId);
    if (!focused) return;
    await tick();
    document.getElementById(sessionOptionId(workflowId))?.parentElement?.querySelector<HTMLElement>(".session-pin")?.focus({ preventScroll: true });
  }

  /* The server lists at most this many running sessions it did not start (app.py
     `_DISCOVERY_LIMIT`); at the limit, a missing one may only be past it. */
  const DISCOVERY_LIMIT = 200;

  /* Every session there is, or nothing: archives are pruned only against a whole list. */
  function wholeList(): string[] | undefined {
    if (!sessionsLoaded || sessionsError) return undefined;
    if (sessions.filter((session) => session.is_discovered).length >= DISCOVERY_LIMIT) return undefined;
    return sessions.map((session) => session.workflow_id);
  }

  /* Into a shut group the row is not drawn, so focus stays on the list rather than the page.
     An open row loses its archive once restored, so focus lands on its pin. */
  async function toggleArchive(workflowId: string, button: HTMLElement): Promise<void> {
    const focused = document.activeElement === button;
    archived = toggleArchivedSession(workflowId, wholeList());
    if (archivedIds.has(workflowId)) onArchiveSession?.(workflowId);
    if (!focused) return;
    await tick();
    const item = document.getElementById(sessionOptionId(workflowId))?.parentElement;
    const moved = item?.querySelector<HTMLElement>(".session-archive") ?? item?.querySelector<HTMLElement>(".session-pin");
    (moved ?? sessionListElement)?.focus({ preventScroll: true });
  }

  function toggleArchiveOpen(): void {
    archiveOpen = !archiveOpen;
    arrowedId = null;
    writeOperatorPrefs({ sessionArchiveOpen: archiveOpen });
  }

  let endTarget = $state<EndTarget | null>(null);

  function sessionName(session: Session): string {
    return `${sessionAgentLabel(session)} — ${sessionTitle(session)}`;
  }

  function askToEnd(session: Session, trigger: HTMLElement): void {
    endTarget = { workflowId: session.workflow_id, name: sessionName(session), trigger };
  }

  /* An ended row loses its X, so focus falls back to the row itself rather than the page. */
  function endDialogClosed(): void {
    const target = endTarget;
    endTarget = null;
    void tick().then(() => {
      if (target && !target.trigger?.isConnected) document.getElementById(sessionOptionId(target.workflowId))?.focus();
    });
  }

</script>

<!-- A status's shape wherever it is drawn: the row, its card and the nav. A gone workflow takes
     StatusChip's closed circle in the quiet closed tone: like a close, nothing more can happen
     to it, but it is no longer there to open, so not the plain closed pip. -->
{#snippet statusGlyph(kind: StatusKind, unavailable: boolean)}
  <span class="session-glyph" aria-hidden="true" style:--status-color={glyphColor(kind)}>
    {#if unavailable}<XCircle size={12} aria-hidden="true" />{:else if ROW_GLYPH_KINDS.has(kind)}<StateGlyph {kind} size={12} />{:else}<span class={["session-pip", kind === "closed" && "hollow"]}></span>{/if}
  </span>
{/snippet}
{#snippet agentIcon()}<IconAgent size={12} />{/snippet}
{#snippet startedIcon()}<Clock size={12} aria-hidden="true" />{/snippet}

{#if onEndSession}
  <EndSessionDialog target={endTarget} onConfirm={onEndSession} onClose={endDialogClosed} />
{/if}

{#if display === "launcher"}
  <div class="session-controls">
    <button
      type="button"
      class="head-button session-new"
      aria-label="New session"
      data-tip="New session"
      data-tip-below
      aria-haspopup="dialog"
      onclick={openNewSessionMenu}
    >
      <Plus size={13} aria-hidden="true" />
    </button>
    <!-- A label, not a control: the kicker takes the status hue the way a pane's kind does.
         A tab stop all the same, so a keyboard can reach the title in its tip. -->
    {#snippet identity()}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <span
        class="head-name session-anchor"
        role="group"
        tabindex="0"
        aria-label={identityName}
        aria-describedby={headline.hint ? "session-status-hint" : undefined}
        data-tip={headline.hint ? "" : identityTip}
        data-tip-below
      >
        <!-- Only with the states that also get words, so it never churns beside the minimap. -->
        {#if gone || ROW_GLYPH_KINDS.has(statusKind)}{@render statusGlyph(statusKind, gone)}{/if}
        <span class="kicker head-kind session-name" dir="auto" style={`--head-accent: var(--${statusTone})`}>{agentTitle}</span>
        {#if spokenStatus}<span class="kicker session-state">{spokenStatus}</span>{/if}
        {#if !spokenStatus && agentTitle !== statusLabel}<span class="session-state-spoken">{statusLabel}</span>{/if}
        <!-- The sentence is this card, not a second line. `data-tip=""` so the text bubble
             does not repeat it. Hidden from the tree; the anchor's description is the one reading. -->
        {#if headline.hint}
          <TipCard
            id="session-status-hint"
            role="tooltip"
            aria-hidden="true"
            heading={activeSession ? sessionTitle(activeSession) : null}
            fields={activeSession ? [{ key: "Agent", value: agentDescription ? `${agentTitle} — ${agentDescription}` : agentTitle }] : []}
            body={headline.hint}
          />
        {/if}
      </span>
    {/snippet}
    {@render identity()}
    {#if sessionId}
      {@const temporalHref = temporalWorkflowUrl(sessionId, globalThis.location?.search, activeSession?.temporal_workflows_url)}
      <a
        class="head-button session-temporal"
        href={temporalHref}
        target="_blank"
        rel="noreferrer noopener"
        aria-label="Open in Temporal Web UI"
        data-tip-below
        data-tip={temporalHref.startsWith(`${DEFAULT_TEMPORAL_UI}/`)
          ? "Open in Temporal Web UI, assuming the local dev server (localhost:8233, namespace default)"
          : "Open in Temporal Web UI"}
      >
        <ExternalLink size={13} aria-hidden="true" />
      </a>
    {/if}
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
  <section class="session-manager" aria-label="Sessions">
    <!-- The pane header's row, so the drawer and the pane beside it share one top edge. -->
    <header class="surface-head">
      <h2 class="head-name"><span class="kicker head-kind">Sessions</span><span class="session-count">{countFormat.format(sessionItems.length)}</span></h2>
      {#if onRefreshSessions}
        <button
          type="button"
          class={["head-button session-refresh", refreshingSessions && "spinning"]}
          aria-label="Refresh sessions"
          data-tip="Refresh sessions"
          data-tip-below
          data-tip-align="end"
          disabled={refreshingSessions}
          onclick={() => void onRefreshSessions?.()}
        ><RefreshCw size={13} /></button>
      {/if}
    </header>
    <div class="session-body">
      <div class="session-filters">
      <label class="field">
        <Search size={14} aria-hidden="true" />
        <input
          {@attach focusFirst}
          bind:this={sessionSearchInput}
          bind:value={sessionSearch}
          placeholder="Search message, agent, or ID"
          aria-label="Search sessions"
          role="combobox"
          aria-autocomplete="list"
          aria-controls="session-listbox"
          aria-expanded="true"
          aria-activedescendant={highlightedId ? sessionOptionId(highlightedId) : undefined}
          oninput={() => (arrowedId = null)}
          use:inputClearDissolve
          onkeydown={handleSearchKeydown}
        />
        {#if sessionSearch}
          <IconButton class="clear-search" label="Clear search" onclick={clearSessionSearch}>
            <X size={13} aria-hidden="true" />
          </IconButton>
        {/if}
      </label>
      {#if agentOptions.length > 1}
        <Select
          bind:value={agentFilter}
          aria-label="Agent"
          onchange={() => (arrowedId = null)}
          options={[{ value: "", label: "All agents" }, ...agentOptions.map((option) => ({ value: option.type, label: option.label }))]}
        />
      {/if}
      </div>
      {#if sessionsError}
        <div class="session-error" role="alert">
          <p>{sessionsError}</p>
          {#if onRefreshSessions}
            <Chip size="xs" fill="quiet" disabled={refreshingSessions} onclick={() => void onRefreshSessions?.()}>Retry</Chip>
          {/if}
        </div>
      {/if}
      <div class="session-list" id="session-listbox" role="listbox" aria-label="Sessions" tabindex="-1" bind:this={sessionListElement} {@attach windowedFocus()}>
        {#if !sessionsError && filteredSessionItems.length === 0}
          <div class="session-empty">
            <strong>{sessionItems.length ? "No matching sessions" : "No sessions yet"}</strong>
            <p>{sessionItems.length ? "Try another search or agent filter." : "Choose New session in the top bar to begin."}</p>
          </div>
        {:else if !sessionsError && sessionGroups.length === 0}
          <!-- Every listed session is archived: the list above the Archived group was blank. -->
          <div class="session-empty"><strong>{sessionSearchTerm || agentFilter ? "Every match is archived" : "Every session is archived"}</strong></div>
        {/if}
        <div class="session-rows" style:height={`${$sessionRows.getTotalSize()}px`}>
        {#each visibleGroups as group (group.label)}
          <div class="session-group" role="group" aria-label={group.label}>
            {#each group.rows as row (row.key)}
              {@const entry = entries[row.index]}
              <div class="session-entry" data-index={row.index} style:top={`${row.start}px`} {@attach measureRow}>
              {#if entry.kind === "heading"}
                <div class="group-heading" aria-hidden="true">{entry.group} <span>{countFormat.format(entry.count)}</span></div>
              {:else if entry.kind === "archive"}
                <!-- The one heading that does something. A sibling of the options in its group,
                     like the pin, never inside one. -->
                <button type="button" class="group-heading archive-toggle disclosure" aria-expanded={archiveOpen} onclick={toggleArchiveOpen}>
                  {entry.group} <span>{countFormat.format(entry.count)}</span>
                  <ChevronRight class="disclosure-chevron" size={12} aria-hidden="true" />
                </button>
              {:else}
              {@const item = entry.item}
              {@const kind = sessionStatusKind(item)}
              {@const age = sessionAge(item.created_at, now)}
              {@const pinned = pinRank.has(item.workflow_id)}
              {@const isArchived = archivedIds.has(item.workflow_id)}
              {@const atRest = isArchived || sessionGone(item)}
              {@const endable = Boolean(onEndSession) && !sessionClosed(item)}
              <!-- Archive is for what is over; an archived row can always be restored. -->
              {@const archivable = isArchived || sessionClosed(item) || sessionGone(item)}
              {@const title = sessionTitle(item)}
              <!-- The row's status shape, and the same shape leading the status line of its card. -->
              {#snippet glyph()}{@render statusGlyph(kind, sessionGone(item))}{/snippet}
              <div class="session-item">
                <!-- The glyph's state and the short age are drawn; the name reads both as words. -->
                <button
                  type="button"
                  id={sessionOptionId(item.workflow_id)}
                  class={["session-row", item.workflow_id === sessionId && "active", kind === "closed" && "closed", arrowedId !== null && item.workflow_id === highlightedId && "highlighted"]}
                  role="option"
                  aria-setsize={filteredIds.length}
                  aria-posinset={entry.ordinal}
                  aria-selected={item.workflow_id === highlightedId}
                  aria-current={item.workflow_id === sessionId ? "true" : undefined}
                  aria-describedby={`${sessionOptionId(item.workflow_id)}-tip`}
                  data-tip=""
                  data-tip-side="right"
                  onclick={() => void openSession(item.workflow_id)}
                >
                  {@render glyph()}
                  <!-- Drawn only in a wide list; the name and the card already read both. -->
                  <span class="session-status-words fade-end" aria-hidden="true" dir="auto">{sessionStatusWords(item)}</span>
                  <span class="session-started fade-end" aria-hidden="true">{sessionStartedShort(item.created_at)}</span>
                  <span class={["session-title fade-end", title === UNTITLED_SESSION && "untitled"]} dir="auto">{title}</span>
                  <span class="visually-hidden">, {sessionStatusLabel(item)},</span>
                  <span class={["session-agent", !showAgent && "visually-hidden", "fade-end"]} dir="auto">{sessionAgentLabel(item)}</span>
                  {#if atRest}<span class="session-kept-gap" aria-hidden="true"></span>{/if}
                  <time aria-hidden="true" datetime={age.short ? new Date(item.created_at * 1000).toISOString() : undefined}>{age.short}</time>
                  {#if age.long}<span class="visually-hidden">, {age.long}</span>{/if}
                  <!-- Hidden from the row's name, which it would otherwise join while shown;
                       aria-describedby still reads it. -->
                  <TipCard
                    id={`${sessionOptionId(item.workflow_id)}-tip`}
                    role="tooltip"
                    aria-hidden="true"
                    style="--tip-card-heading-lines: 3"
                    heading={title}
                    fields={sessionTipFields(item, age.long, glyph)}
                  />
                </button>
                <!-- Beside the option, never in it: an option is a button already. Tab reaches
                     them in the order drawn, after the option. An open row offers pin and End,
                     a closed one pin and archive: the pin first, like a pane header. A pin is
                     only drawn on hover: Pinned already says it. -->
                <div class="session-actions">
                <!-- A toggle, so it says which way it is. -->
                <IconButton
                  class="session-pin"
                  label={`${pinned ? "Unpin session" : "Pin session"} — ${sessionName(item)}`}
                  tip={pinned ? "Unpin session" : "Pin session"}
                  pressed={pinned}
                  data-tip-align="end"
                  onclick={(event) => void togglePin(item.workflow_id, event.currentTarget as HTMLElement)}
                >
                  <Pin size={14} fill={pinned ? "currentColor" : "none"} aria-hidden="true" />
                </IconButton>
                {#if endable}
                  <IconButton
                    class="session-end"
                    label={`End session — ${sessionName(item)}`}
                    data-tip-align="end"
                    onclick={(event) => askToEnd(item, event.currentTarget as HTMLElement)}
                  >
                    <X size={14} />
                  </IconButton>
                {/if}
                <!-- Last, so an archive drawn at rest always sits beside the age, End or not.
                     Shown at rest on an archived or gone row, so a restart's worth of them clears quickly. -->
                {#if archivable}
                  <IconButton
                    class={atRest ? "session-archive at-rest" : "session-archive"}
                    label={`${isArchived ? "Unarchive session" : "Archive session"} — ${sessionName(item)}`}
                    tip={isArchived ? "Unarchive session" : "Archive session"}
                    pressed={isArchived}
                    data-tip-align="end"
                    onclick={(event) => void toggleArchive(item.workflow_id, event.currentTarget as HTMLElement)}
                  >
                    {#if isArchived}<ArchiveRestore size={14} aria-hidden="true" />{:else}<Archive size={14} aria-hidden="true" />{/if}
                  </IconButton>
                {/if}
                </div>
              </div>
              {/if}
              </div>
            {/each}
          </div>
        {/each}
        </div>
      </div>
    </div>
  </section>
{/if}

<style>
  .session-controls {
    flex: 0 1 auto;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
  }

  /* Sized to its text rather than the slack, so the Web UI link stays beside it; its own
     padding stands in for the inset a kind label has at a header's edge. */
  .session-anchor {
    flex: 0 1 auto;
    min-width: 0;
    padding-inline: var(--gap-xs);
  }

  .session-anchor:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* An agent label has no length limit, so it gives way before the status does; the tip
     carries it whole. Shrinks first, and the status only after this is gone, so a long
     name cannot push the link over the rest of the nav. */
  .session-name {
    flex: 0 1000 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* The other statuses only tint the pip, so a reader who cannot see it hears them here. */
  .session-state-spoken {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }

  /* A few words a reader has to act on, so the name gives way to it. Shrinking at all lost it
     a few letters ("1 APPROVAL NEEDE…") while the name still had room, so it doesn't; the cap
     keeps a long one from painting over the mark. */
  .session-state {
    flex: none;
    max-width: 24ch;
    overflow: hidden;
    white-space: nowrap;
    padding-inline-start: var(--gap-sm);
    border-inline-start: 1px solid var(--border);
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
    text-overflow: ellipsis;
  }

  /* Full-bleed, so its header meets the drawer's edges; the body below takes the inset. */
  .session-manager {
    flex: 1;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: var(--surface-1);
  }

  .session-count {
    min-width: 0;
    overflow: hidden;
    color: var(--text-3);
    font-size: var(--font-lg);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .session-body {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: var(--pane-inset);
    padding: var(--pane-inset);
  }

  /* One line where the drawer is wide enough for both, two where it is not. */
  .session-filters {
    display: flex;
    flex-wrap: wrap;
    gap: var(--gap-sm);
  }

  .session-filters > .field {
    flex: 1 1 12rem;
  }

  .session-filters > :global(.field.select) {
    flex: 0 1 11rem;
  }

  /* A size container, so a row's tip is never wider than the list that clips it. */
  .session-list {
    /* SESSION_ROW_HEIGHT in sessionHistory.ts: the windowed list estimates a row as this. */
    --session-row-height: var(--control-height);
    /* A row action's hit target: 24px, or 32px for a finger. */
    --session-action-width: calc(var(--session-row-height) - var(--gap-xs));
    /* The widest short age, "59m", at --font-sm in tabular numbers: no dead space before "1h". */
    --session-age-width: 24px;
    /* "Closed · completed" and "Sep 30, 12:59 PM" whole at --font-sm; a longer status fades
       after "Needs attention" until the list is wide enough for it. */
    --session-status-width: 104px;
    --session-started-width: 96px;
    container-type: inline-size;
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }

  /* A finger's row. */
  @media (pointer: coarse) {
    .session-list {
      --session-row-height: 36px;
    }
  }

  .session-rows {
    position: relative;
  }

  /* No box of its own: its rows are placed against .session-rows, and it stays only to
     name the day to assistive tech. */
  .session-group {
    display: contents;
  }

  .session-entry {
    position: absolute;
    inset-inline: 0;
  }

  /* SESSION_HEADING_HEIGHT, starting where a row's glyph does. */
  .group-heading {
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    min-height: var(--control-height-xs);
    margin: 0;
    padding-inline: var(--gap-md);
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

  /* Pin, then End or archive, just before the age, which stays in sight. Laid over the end of the
     title and agent rather than given a track of its own: three slots reserved on every row
     left a 240px drawer about 70px of title. A fade in the row's own tint leads it, so the
     text gives way cleanly, and nothing under it moves when it shows. The targets abut, a
     24px pitch like Cursor's, and the row's own gap-sm separates the last from the age. An
     archived or gone row keeps its archive painted at rest over a track of its own
     (`.session-kept-gap`), so it never sits on top of text. */
  .session-actions {
    --actions-bg: var(--surface-1);
    /* The row text's own `.fade-end` width, which is scoped to that text. */
    --fade-width: 24px;
    position: absolute;
    inset-block: 0;
    inset-inline-end: calc(var(--gap-xs) + var(--session-age-width) + var(--gap-sm));
    display: flex;
    padding-inline-start: var(--fade-width);
    pointer-events: none;
  }

  .session-item:is(:hover, :focus-within) .session-actions {
    background: linear-gradient(to right, transparent, var(--actions-bg) var(--fade-width));
  }

  .session-item:is(:hover, :focus-within) .session-actions:dir(rtl) {
    background: linear-gradient(to left, transparent, var(--actions-bg) var(--fade-width));
  }

  .session-item:has(.session-row.highlighted) .session-actions {
    --actions-bg: var(--control-hover);
  }

  /* After the highlight, as the row's own fill is: the row opened is highlighted too. */
  .session-item:has(.session-row.active) .session-actions {
    --actions-bg: color-mix(in srgb, var(--accent) 14%, var(--surface-1));
  }

  /* Doubled class to outrank IconButton's own hover, which would bring its border back. */
  .session-actions :global(.icon-button.icon-button) {
    width: var(--session-action-width);
    height: auto;
    /* A button's default padding would squeeze the 14px glyph to 10px. */
    padding: 0;
    border-color: transparent;
    background: transparent;
    color: var(--text-3);
    opacity: 0;
    pointer-events: auto;
    transition: opacity var(--duration-fast) var(--ease-ui);
  }

  .session-item:is(:hover, :focus-within) .session-actions :global(.icon-button),
  .session-actions :global(.icon-button.at-rest) {
    opacity: 1;
  }

  .session-actions :global(.icon-button.icon-button.pressed) {
    color: var(--text-2);
  }

  .session-item .session-actions :global(.icon-button.icon-button:hover),
  .session-item .session-actions :global(.icon-button.icon-button:focus-visible) {
    border-color: transparent;
    background: var(--surface-3);
    color: var(--text-1);
  }

  .session-item .session-actions :global(.session-end.icon-button:hover),
  .session-item .session-actions :global(.session-end.icon-button:focus-visible) {
    color: var(--error);
  }

  /* No hover to reveal them, so they are always shown. */
  @media (hover: none) {
    .session-actions {
      background: linear-gradient(to right, transparent, var(--actions-bg) var(--fade-width));
    }

    .session-actions:dir(rtl) {
      background: linear-gradient(to left, transparent, var(--actions-bg) var(--fade-width));
    }

    .session-actions :global(.icon-button) {
      opacity: 1;
    }
  }

  .archive-toggle {
    width: 100%;
    border: 0;
    background: transparent;
    font-family: inherit;
    text-align: start;
    cursor: pointer;
    transition: background var(--duration-fast) var(--ease-ui);
  }

  .archive-toggle:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* No glyph for the caret to stand in for, so it is always drawn. */
  .archive-toggle :global(.disclosure-chevron) {
    opacity: 1;
  }

  /* One line at a fixed height, so the list's estimate is the measurement: a minimum, so
     zoomed text grows the row and the list measures it. Zero minimums all the way down, so
     an unbroken title or agent name truncates instead of widening the row. */
  .session-row {
    min-width: 0;
    min-height: var(--session-row-height);
    display: flex;
    align-items: center;
    gap: var(--gap-sm);
    padding-block: 0;
    padding-inline: var(--gap-md) var(--gap-xs);
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: start;
    transition: background var(--duration-fast) var(--ease-ui);
  }

  .session-row:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  .session-row.highlighted {
    background: var(--control-hover);
  }

  .session-row.active {
    background: var(--selected-bg);
  }

  /* One width for every glyph, so every title starts at the same edge. */
  .session-glyph {
    flex: none;
    width: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--status-color);
  }

  .session-pip {
    width: var(--pip);
    height: var(--pip);
    background: currentColor;
  }

  .session-pip.hollow {
    background: transparent;
    box-shadow: inset 0 0 0 1px currentColor;
  }

  /* `match-parent`: text in the other direction starts at the row's start, not across a gap. */
  .session-title {
    flex: 1 1 0;
    min-width: 0;
    color: var(--text-1);
    font-size: var(--font-lg);
    text-align: match-parent;
  }

  .session-title.untitled {
    color: var(--text-3);
  }

  .session-row.active .session-title {
    font-weight: 500;
  }

  .session-row.closed .session-title {
    color: var(--text-3);
  }

  /* Whole up to 40% of the row, where it gives way; the tip carries it whole. A title starts
     from nothing, so a long one cannot squeeze this to a letter. */
  .session-agent {
    flex: 0 1 auto;
    min-width: 0;
    max-width: 40%;
    color: var(--text-3);
    font-size: var(--font-sm);
    text-align: match-parent;
  }

  /* Too narrow for both: the title keeps the row, and the agent is read and in the card. */
  @container (width < 300px) {
    .session-agent {
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
  }

  /* What a wide drawer has room for, in fixed widths so they line up down the list like a
     table. Ahead of the title, so the hover actions over the title's end never cover them. */
  .session-status-words,
  .session-started {
    display: none;
    flex: none;
    color: var(--text-3);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    text-align: match-parent;
  }

  .session-status-words {
    width: var(--session-status-width);
  }

  .session-started {
    width: var(--session-started-width);
  }

  @container (width >= 480px) {
    .session-status-words {
      display: block;
    }
  }

  /* Room enough here for the longest status whole, "Needs attention · terminated". */
  @container (width >= 640px) {
    .session-rows {
      --session-status-width: 152px;
    }

    .session-started {
      display: block;
    }
  }

  /* The archive's slot on an archived or gone row, the one action drawn at rest: with the
     row's gaps either side, one target and the same gap-sm before the age. The archive is
     always last, so it is the one by the age; the rest of the cluster, End included on an
     archived row still running, lays over the text on hover, faded, like any row's. */
  .session-kept-gap {
    flex: none;
    width: calc(var(--session-action-width) - var(--gap-sm));
  }

  time {
    flex: none;
    width: var(--session-age-width);
    color: var(--text-3);
    font-size: var(--font-sm);
    font-variant-numeric: tabular-nums;
    text-align: end;
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

  .session-refresh.spinning :global(svg) {
    animation: session-refresh-spin 800ms linear infinite;
  }

  @keyframes session-refresh-spin {
    to {
      transform: rotate(360deg);
    }

  }

  @media (hover: hover) and (pointer: fine) {
    .session-item:hover .session-row,
    .archive-toggle:hover {
      background: var(--control-hover);
    }

    /* The row's hover tint wins over its selection, so the fade's must too. */
    .session-item:hover:has(.session-row) .session-actions {
      --actions-bg: var(--control-hover);
    }

  }

  @media (prefers-reduced-motion: reduce) {
    .session-refresh.spinning :global(svg) {
      animation: none;
      opacity: 0.6;
    }

    .session-row,
    .session-actions :global(.icon-button.icon-button),
    .archive-toggle {
      transition: none;
    }

  }
</style>
