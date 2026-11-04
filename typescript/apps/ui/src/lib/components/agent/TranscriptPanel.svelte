<script lang="ts">
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import { ListFilter, ScrollText, Search, X, XCircle } from "@lucide/svelte";
  import { rowIcon } from "$lib/components/flow/WorkflowTimeline.svelte";
  import { createVirtualizer } from "@tanstack/svelte-virtual";
  import { tick, untrack } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import Chip, { type ChipTone } from "$lib/components/primitives/Chip.svelte";
  import IconButton from "$lib/components/primitives/IconButton.svelte";
  import type { StatusKind } from "$lib/components/primitives/StatusChip.svelte";
  import TraceImages from "$lib/components/primitives/TraceImages.svelte";
  import TraceRow from "$lib/components/primitives/TraceRow.svelte";
  import Copyable from "$lib/components/primitives/Copyable.svelte";
  import LongContent from "$lib/components/primitives/LongContent.svelte";
  import { citationAriaLabel, citationLinks, markdownToText } from "$lib/components/chat/MarkdownMessage.svelte";
  import { windowedFocus } from "$lib/components/primitives/windowedFocus";
  import WindowedRows from "$lib/components/primitives/WindowedRows.svelte";
  import { scrollFollower } from "$lib/state/followScroll";
  import { formatLogValue } from "$lib/state/logValue";
  import { toolImages } from "$lib/state/toolImages";
  import { dissolveClear, inputClearDissolve } from "$lib/motion/inputClearDissolve";
  import {
    formatDuration,
    formatElapsedDuration,
    formatTimestamp,
    plural,
    rowIdentifiers,
    statusNote,
    type ReplayActor,
    type ReplayLogRow,
    type TurnLogGroup,
    type TurnLogSummary
  } from "$lib/state/replayLog";
  import { rowHasDetails, type TraceStep } from "$lib/state/replyStatus";
  import { STEP_GLYPHS, logLines, type LogLine } from "$lib/state/logTree";
  import { formatTokens } from "$lib/cost/pricing";
  import { BrainCircuit, MessageSquare, Radio } from "@lucide/svelte";

  export type TranscriptFilter = "all" | "model" | "tool" | "approval" | "error";

  /* A folded line: --control-height, with no rule under it. */
  const LINE_HEIGHT = 28;

  const SHORT_TIME: Intl.DateTimeFormatOptions = { hour: "numeric", minute: "2-digit" };
  const FULL_TIME: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "medium" };
  const compactTokens = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

  interface Props {
    groups: TurnLogGroup[];
    activeTurnNumber: number | null;
    activeRowId?: string | null;
    activeOrdinal: number | null;
    filter?: TranscriptFilter;
    onFilterChange?: (filter: TranscriptFilter) => void;
    /**
     * A row to bring to the reader: a State flow card's "view in logs". The nonce makes
     * asking for the same row twice a second request rather than no change.
     */
    reveal?: { rowId: string; nonce: number } | null;
    /**
     * Whether the whole run has any event, not just the run as of the cursor. `groups` is
     * the run as of the cursor, so gating the filters on it would drop them every time the
     * reader scrubbed back to the first step.
     */
    runHasEvents?: boolean;
  }

  let {
    groups,
    activeTurnNumber,
    activeRowId = null,
    activeOrdinal,
    filter = "all",
    onFilterChange,
    reveal = null,
    runHasEvents = true
  }: Props = $props();
  let expandedRows = $state<Record<string, boolean>>({});
  /* Heads the reader folded or opened, by step id; any other keeps logLines' default. */
  let collapsedSteps = $state<Record<string, boolean>>({});
  let query = $state("");
  let searchInput = $state<HTMLInputElement | null>(null);

  function clearSearch(): void {
    dissolveClear(searchInput, () => (query = ""));
    searchInput?.focus({ preventScroll: true });
  }

  function handleSearchKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || event.isComposing || !query) return;
    event.preventDefault();
    clearSearch();
  }

  const filters: Array<{ key: TranscriptFilter; label: string }> = [
    { key: "all", label: "All" },
    { key: "model", label: "Model" },
    { key: "tool", label: "Tools" },
    { key: "approval", label: "Approvals" },
    { key: "error", label: "Errors" }
  ];

  function matchesFilter(row: ReplayLogRow, key: TranscriptFilter): boolean {
    if (key === "all") return true;
    if (key === "model") return row.actor === "model" || row.actor === "reasoning";
    if (key === "tool") return row.actor === "tool";
    if (key === "approval") return row.actor === "approval";
    if (key === "error") return row.tone === "error" || row.actor === "error";
    return true;
  }

  interface PrimaryPayload {
    label: string;
    text: string;
  }

  function inputText(row: ReplayLogRow): string {
    return formatLogValue(row.input);
  }

  function primaryPayload(row: ReplayLogRow): PrimaryPayload | null {
    const output = formatLogValue(row.output);
    if (output) return { label: "output", text: output };

    return null;
  }

  function fullLogDetail(row: ReplayLogRow): string {
    const sections: string[] = [];
    const body = formatLogValue(row.body);
    const detail = formatLogValue(row.detail);
    const input = inputText(row);
    const output = formatLogValue(row.output);
    const citations = row.citations
      .map(
        (citation) =>
          citation.custom_metadata?.deep_url ??
          citation.document_uri ??
          citation.file_name ??
          "Source"
      )
      .filter(Boolean);
    const metadata = [
      `event: ${row.event}`,
      `actor: ${row.actor}`,
      `turn: ${row.turnNumber}`,
      `source_turn: ${row.sourceTurnNumber}`,
      row.sourceLabel ? `source: ${row.sourceLabel}` : "",
      row.workflowId ? `workflow_id: ${row.workflowId}` : "",
      row.status ? `status: ${row.status}` : "",
      row.toolName ? `tool: ${row.toolName}` : "",
      row.toolId ? `tool_id: ${row.toolId}` : "",
      row.model ? `model: ${row.model}` : "",
      `timestamp: ${formatTimestamp(row.timestamp)}`
    ].filter(Boolean);

    if (output) sections.push(`output:\n${output}`);
    if (body) sections.push(`message:\n${body}`);
    if (detail) sections.push(`detail:\n${detail}`);
    if (input) sections.push(`input:\n${input}`);
    if (citations.length > 0) sections.push(`citations:\n${citations.join("\n")}`);
    sections.push(metadata.join("\n"));

    return sections.join("\n\n");
  }

  function matchesQuery(row: ReplayLogRow, needle: string): boolean {
    if (!needle) return true;
    const haystack = [
      row.label,
      row.body,
      row.detail,
      row.status,
      row.toolName,
      row.output,
      inputText(row)
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  }

  const filtering = $derived(filter !== "all" || query.trim().length > 0);

  /* Unfiltered, the groups pass through as they are, so a commit that touched one turn
     leaves every other turn's object, and so its rendered section, alone. */
  const visibleGroups = $derived(
    !filtering ? groups : groups
      .map((group) => {
        const needle = query.trim().toLowerCase();
        const rows = group.rows.filter((row) => matchesFilter(row, filter) && matchesQuery(row, needle));
        return { ...group, rows };
      })
      .filter((group) => group.rows.length > 0)
  );

  const totalRows = $derived(
    groups.reduce((sum, group) => sum + group.rows.length, 0)
  );
  const shownRows = $derived(
    visibleGroups.reduce((sum, group) => sum + group.rows.length, 0)
  );

  /* By a turn's rows, so a commit to one turn leaves every other turn's lines as they were, and
     only for turns that are drawn or revealed. */
  const lineCache = new WeakMap<
    readonly ReplayLogRow[],
    {
      collapsed: Record<string, boolean>;
      filtering: boolean;
      lines: LogLine[];
      offsets: Map<string, string>;
      /** The widest offset in the turn, in characters, so every row's column is that wide. */
      offsetWidth: number;
      /** The step each row is an event of, for its details. */
      stepOf: Map<string, TraceStep>;
    }
  >();
  function cachedLines(group: TurnLogGroup) {
    const hit = lineCache.get(group.rows);
    if (hit && hit.collapsed === collapsedSteps && hit.filtering === filtering) return hit;
    const lines = logLines(group.rows, collapsedSteps, filtering);
    const offsets = lineOffsets(lines, group.startedAt);
    const stepOf = new Map<string, TraceStep>();
    for (const line of lines) {
      if (line.kind === "step") for (const row of line.step.rows) stepOf.set(row.id, line.step);
    }
    const offsetWidth = Math.max(4, ...[...offsets.values()].map((offset) => offset.length));
    const entry = { collapsed: collapsedSteps, filtering, lines, offsets, offsetWidth, stepOf };
    lineCache.set(group.rows, entry);
    return entry;
  }
  const linesFor = (group: TurnLogGroup): LogLine[] => cachedLines(group).lines;

  const lineTime = (line: LogLine) => (line.kind === "step" ? line.step.startedAt : line.row.timestamp);

  /**
   * Each line's time as an offset into its turn, by line id, only where it differs from the line
   * above: a run of events in the same second reads as one time, and the turn's own start, which
   * the header already says, reads as none. The wall clock is in each row's tip.
   */
  function lineOffsets(lines: readonly LogLine[], startedAt: number): Map<string, string> {
    const offsets = new Map<string, string>();
    let previous = `+${formatDuration(0)}`;
    for (const line of lines) {
      const time = lineTime(line);
      if (!Number.isFinite(time) || !Number.isFinite(startedAt)) continue;
      const offset = `+${formatDuration(time - startedAt)}`;
      if (offset !== previous) offsets.set(line.id, offset);
      previous = offset;
    }
    return offsets;
  }

  /** The bookkeeping around a turn: a message arriving, a handler or turn opening and closing, a
      reply streaming. Drawn quiet, so the calls, gates, subagents and errors stand out. */
  function lifecycle(actor: ReplayActor): boolean {
    switch (actor) {
      case "user":
      case "agent":
      case "queue":
      case "system":
        return true;
      case "model":
      case "tool":
      case "approval":
      case "reasoning":
      case "subagent":
      case "error":
        return false;
      default: {
        const unhandled: never = actor;
        throw new Error(`Unknown log actor ${String(unhandled)}`);
      }
    }
  }

  /** Signal is drawn bright beside its glyph, lifecycle dim, and the cursor's row never dim. */
  function lineTone(line: LogLine, active: boolean): "signal" | "routine" | null {
    if (line.signal) return "signal";
    if (active || line.kind === "step") return null;
    return lifecycle(line.row.actor) ? "routine" : null;
  }

  interface TurnFigure {
    key: "model" | "tool" | "approval" | "error" | "tokens";
    value: string;
    words: string;
  }

  /** A turn's counts, each read in full by its name and tip; a zero is not drawn. Tokens come last,
      so they are what fades when the pane is narrower still. */
  function turnFigures(summary: TurnLogSummary): TurnFigure[] {
    const figures: Array<TurnFigure | null> = [
      summary.modelCalls ? { key: "model", value: String(summary.modelCalls), words: plural(summary.modelCalls, "model call") } : null,
      summary.toolCalls ? { key: "tool", value: String(summary.toolCalls), words: plural(summary.toolCalls, "tool call") } : null,
      summary.approvals ? { key: "approval", value: String(summary.approvals), words: plural(summary.approvals, "approval") } : null,
      summary.errors ? { key: "error", value: String(summary.errors), words: plural(summary.errors, "error") } : null,
      summary.tokens ? { key: "tokens", value: `${compactTokens.format(summary.tokens)} tok`, words: plural(summary.tokens, "token") } : null
    ];
    return figures.filter((figure) => figure != null);
  }

  function figureIcon(key: TurnFigure["key"]): typeof XCircle | null {
    switch (key) {
      case "model":
      case "tool":
      case "approval":
        return rowIcon({ kind: key });
      case "error":
        return XCircle;
      case "tokens":
        return null;
      default: {
        const unhandled: never = key;
        throw new Error(`No icon for turn figure ${String(unhandled)}`);
      }
    }
  }

  /** A step's one figure: a model call's tokens, or the duration of a step with nothing else to
      say. The duration it does not show goes in its tip, after the wall clock. */
  function stepCells(step: TraceStep): { figure?: string; note?: string; tip: string } {
    const duration = step.durationMs ? formatElapsedDuration(step.durationMs) : "";
    const tokens = step.kind === "model" && step.note ? step.note : "";
    const note = tokens ? "" : step.note;
    const figure = tokens || (note ? "" : duration);
    const tip = [formatTimestamp(step.startedAt), duration && duration !== figure ? `took ${duration}` : ""];
    return { figure: figure || undefined, note: note || undefined, tip: tip.filter(Boolean).join(" · ") };
  }

  /** The cursor's row, or the folded head it is inside. */
  function holdsActive(line: LogLine): boolean {
    if (line.kind === "step") return line.collapsed && activeRowId != null && line.rowIds.has(activeRowId);
    return activeRowId === line.row.id || (activeRowId == null && activeOrdinal === line.row.ordinal);
  }

  const lineElementId = (line: LogLine) => (line.kind === "row" ? `log-row-${line.row.id}` : `log-${line.id}`);

  /* The pane's own scroller, and the only box this component is allowed to move.
     See followScroll.ts: `scrollIntoView` was moving the whole pane rail sideways. */
  let itemsElement = $state<HTMLElement | null>(null);
  const follower = scrollFollower(() => itemsElement);

  /* Only the turns in view are in the DOM: a 5,000-message run drew some 20,000 rows. A
     turn is one item, measured as drawn, so its header and rows keep their markup. */
  const turns = createVirtualizer<HTMLElement, HTMLElement>({
    /* Seeded here as well as in the effect below, which SSR never runs. */
    count: untrack(() => visibleGroups.length),
    getItemKey: (index) => visibleGroups[index]?.turnNumber ?? index,
    estimateSize: (index) => 60 + LINE_HEIGHT * (visibleGroups[index]?.rows.length ?? 0),
    getScrollElement: () => itemsElement,
    initialRect: { width: 0, height: 800 },
    gap: 8,
    overscan: 2
  });
  /* Untracked: setOptions publishes the store, which would re-run the effect calling it. */
  const turnList = () => untrack(() => $turns);
  const measureTurn: Attachment<HTMLElement> = (node) => turnList().measureElement(node);
  const turnFocus = windowedFocus(
    (index) => turnList().scrollToIndex(index, { align: "auto" }),
    () => visibleGroups.length
  );

  /* Pre, so a shrunk list's count lands before the turns render and index past its end. */
  $effect.pre(() => {
    const count = visibleGroups.length;
    itemsElement;
    turnList().setOptions({ count });
  });

  /* Each drawn turn's rows, by turn number, to reveal a row the turn has not drawn. */
  const rowLists: Record<number, { scrollToIndex(index: number, align?: "auto" | "start"): void; measure(): void } | null> = {};

  function remeasureLogRow(turnNumber: number): void {
    rowLists[turnNumber]?.measure();
    turnList().measure();
  }

  const activeGroupIndex = () => visibleGroups.findIndex((group) => group.rows.some((row) => row.id === activeRowId));

  function revealActiveTurn(): void {
    const index = activeGroupIndex();
    if (index < 0) return;
    const group = visibleGroups[index];
    const lineIndex = linesFor(group).findIndex(holdsActive);
    const revealRow = () => rowLists[group.turnNumber]?.scrollToIndex(lineIndex);
    if (rowLists[group.turnNumber]) return revealRow();
    turnList().scrollToIndex(index, { align: "auto" });
    requestAnimationFrame(revealRow);
  }

  /* Follow the cursor's row; do not open it. Opening it is what made a scrub look
     broken: every tick closed one detail block and opened another, and those blocks are
     tall — on the mock run alone the cursor's `Full details` measures 130..350px, so a
     single tick moved up to 700px of content past the reader while the follower chased a
     row whose position had just changed under it. The cursor's row is already marked by
     `.active-row`, with its turn identified by its tinted header; it does not
     also have to be the one row the reader is allowed to have open. LatencyWaterfall
     follows the same playhead and has only ever followed. */
  $effect(() => {
    if (activeRowId == null) return;
    /* Untracked: only the cursor moving may scroll the pane. Folding a head or typing a search
       re-ran this and pulled a reader who had scrolled away back to the cursor. */
    const target = untrack(() => {
      const group = visibleGroups[activeGroupIndex()];
      const line = group && linesFor(group).find(holdsActive);
      return line ? lineElementId(line) : `log-row-${activeRowId}`;
    });
    follower.to(target, revealActiveTurn);
  });

  /**
   * A State flow card's "view in logs". Unlike following the cursor it was asked for, so it
   * clears a filter or search that would hide the row, brings its turn into the drawn window,
   * and then reveals it the way a call's trail does: open, marked, and scrolled to even if the
   * reader had scrolled away. It never moves the replay cursor.
   */
  async function revealLogRow(rowId: string): Promise<void> {
    const row = groups.flatMap((group) => group.rows).find((item) => item.id === rowId);
    if (!row) return;
    if (!matchesFilter(row, filter)) onFilterChange?.("all");
    const needle = query.trim().toLowerCase();
    if (needle && !matchesQuery(row, needle)) query = "";
    await tick();
    const index = visibleGroups.findIndex((group) => group.rows.some((item) => item.id === rowId));
    if (index < 0) return;
    const group = visibleGroups[index];
    /* Only a drawn turn has rows to scroll to; revealRow settles the rest. */
    if (!rowLists[group.turnNumber]) turnList().scrollToIndex(index, { align: "start" });
    /* Every call that folds the row away, outermost first: a subagent's events sit inside the
       parent's call to it, and its own model call folds them again. */
    let call: TraceStep | null = null;
    for (let depth = 0; depth < 8; depth += 1) {
      const folded = linesFor(group).find(
        (line) => line.kind === "step" && line.collapsed && line.rowIds.has(rowId)
      );
      if (folded?.kind !== "step") break;
      call = folded.step;
      collapsedSteps = { ...collapsedSteps, [folded.step.id]: false };
      await tick();
    }
    revealRow(group, call, rowId);
  }

  $effect(() => {
    const target = reveal;
    if (!target) return;
    untrack(() => void revealLogRow(target.rowId));
  });

  function isRowExpanded(rowId: string): boolean {
    return expandedRows[rowId] ?? false;
  }

  /** One row open at a time: opening another closes the one before, so details never pile up
      down the pane. */
  function toggleRow(rowId: string): void {
    expandedRows = isRowExpanded(rowId) ? {} : { [rowId]: true };
  }

  function toggleStep(line: Extract<LogLine, { kind: "step" }>): void {
    collapsedSteps = { ...collapsedSteps, [line.step.id]: !line.collapsed };
  }

  /* The row a trail or a call's event list last took the reader to, marked so they can see where
     they landed even when the event has nothing to open. */
  let revealedRowId = $state<string | null>(null);

  /** Unfold a call, open one of its events and bring that row into view: a folded call's trail
      icon, an event in an open row's call, or a State flow card's row (which may be in no call). */
  function revealRow(group: TurnLogGroup, step: TraceStep | null, rowId: string): void {
    if (step) collapsedSteps = { ...collapsedSteps, [step.id]: false };
    revealedRowId = rowId;
    if (!isRowExpanded(rowId)) toggleRow(rowId);
    /* The windowed rows place it from estimates, and unfolding the call just changed every
       height after it, so the first jump can miss. Settle it over a few frames: jump to the
       row until it is drawn, then put it a third of the way down the pane until it holds.
       Only this pane's own scroller moves (see followScroll.ts). */
    let frames = 0;
    let held = 0;
    const settle = () => {
      const box = itemsElement;
      if (!box || frames++ > 12 || held >= 2) return;
      const row = document.getElementById(`log-row-${rowId}`);
      if (!row) {
        const index = linesFor(group).findIndex((line) => line.kind === "row" && line.row.id === rowId);
        if (index >= 0) rowLists[group.turnNumber]?.scrollToIndex(index);
      } else {
        const view = box.getBoundingClientRect();
        const rect = row.getBoundingClientRect();
        const inView = rect.top >= view.top && rect.bottom <= view.bottom;
        held = inView ? held + 1 : 0;
        if (!inView) box.scrollTop += rect.top - view.top - view.height / 3;
      }
      requestAnimationFrame(settle);
    };
    void tick().then(settle);
  }

  /** A run of the same event, drawn once with its count: thirty progress events are one icon. */
  function eventRuns(rows: readonly ReplayLogRow[]): Array<{ row: ReplayLogRow; count: number; ids: string[] }> {
    const runs: Array<{ row: ReplayLogRow; count: number; ids: string[] }> = [];
    for (const row of rows) {
      const last = runs.at(-1);
      if (last && last.row.event === row.event) {
        last.count += 1;
        last.ids.push(row.id);
      } else runs.push({ row, count: 1, ids: [row.id] });
    }
    return runs;
  }

  const eventWords = (row: ReplayLogRow) => row.event.replaceAll("_", " ");

  /** Who an event is from, as a hue (its bar and trail icon) and the chip tone of the same hue. */
  const ACTOR_HUE: Record<ReplayActor, string> = {
    user: "var(--queue)",
    queue: "var(--queue)",
    agent: "var(--queue)",
    system: "var(--text-4)",
    model: "var(--model)",
    reasoning: "var(--reasoning)",
    tool: "var(--tool)",
    approval: "var(--live)",
    subagent: "var(--accent)",
    error: "var(--error)"
  };
  const ACTOR_TONE: Record<ReplayActor, ChipTone> = {
    user: "queue",
    queue: "queue",
    agent: "queue",
    system: "neutral",
    model: "model",
    reasoning: "reasoning",
    tool: "tool",
    approval: "live",
    subagent: "accent",
    error: "error"
  };
  const actorHue = (actor: ReplayActor) => ACTOR_HUE[actor];

  function actorIcon(actor: ReplayActor): typeof XCircle {
    switch (actor) {
      case "user":
      case "queue":
      case "agent":
        return MessageSquare;
      case "system":
        return Radio;
      case "reasoning":
        return BrainCircuit;
      case "error":
        return XCircle;
      case "model":
      case "tool":
      case "approval":
      case "subagent":
        return rowIcon({ kind: actor });
      default: {
        const unhandled: never = actor;
        throw new Error(`No icon for log actor ${String(unhandled)}`);
      }
    }
  }

  const rowHue = (row: ReplayLogRow) => (row.tone === "error" ? "var(--error)" : actorHue(row.actor));
  const rowTone = (row: ReplayLogRow) => (row.tone === "error" ? "error" : ACTOR_TONE[row.actor]);

  /** A trail icon's color: how the event ended, once it has, else who it is from. */
  function trailHue(row: ReplayLogRow): string {
    const glyph = rowGlyph(row);
    if (glyph === "error") return "var(--error)";
    if (glyph === "complete") return "var(--success)";
    if (glyph === "approval") return "var(--live)";
    return actorHue(row.actor);
  }

  /**
   * The state a row's glyph draws, or null for a row whose state is only its label. A log
   * row is one event, not a step still in motion: a "started" row stays in the log long
   * after its step ends, so no row spins.
   */
  function rowGlyph(row: ReplayLogRow): StatusKind | null {
    if (row.tone === "error" || row.actor === "error") return "error";
    if (row.tone === "done") return "complete";
    if (row.tone === "approval") return "approval";
    return null;
  }

  /** The tool or model an event belongs to, unless the label already names it. */
  function rowName(row: ReplayLogRow): string | undefined {
    const name = row.toolName || (row.actor === "model" ? row.model : null);
    if (!name) return undefined;
    return row.label.toLowerCase().includes(name.toLowerCase()) ? undefined : name;
  }

  const rowFigure = (row: ReplayLogRow) => (row.usage?.total ? `${formatTokens(row.usage.total)} tok` : undefined);

  /** The row's muted context: a status neither the glyph nor its tokens already say, its source. */
  function rowNote(row: ReplayLogRow, glyph: StatusKind | null, status: string | null): string | undefined {
    const parts = [
      row.usage?.total || glyph ? null : status,
      row.parentTurnNumber != null ? `subagent turn ${row.sourceTurnNumber}` : null
    ].filter(Boolean);
    return parts.length ? parts.join(" · ") : undefined;
  }

</script>

<section class="transcript" aria-label="Replay logs">
  <!-- Nothing to filter or search in a run with no events, so its empty state stands alone. -->
  {#if runHasEvents}
    <div class="transcript-controls">
      <div class="filter-row">
        <div class="filter-chips" role="group" aria-label="Filter logs">
          {#each filters as item (item.key)}
            <Chip
              label={item.label}
              tone="accent"
              size="xs"
              fill="quiet"
              active={filter === item.key}
              aria-pressed={filter === item.key}
              onclick={() => onFilterChange?.(item.key)}
            />
          {/each}
        </div>
        <span class="event-count" dir="auto">
          {filtering ? `${shownRows}/${plural(totalRows, "event")}` : plural(totalRows, "event")}
        </span>
      </div>
      <div class="field">
        <Search size={14} aria-hidden="true" />
        <input
          type="search"
          placeholder="Search events"
          bind:this={searchInput}
          bind:value={query}
          aria-label="Search log events"
          use:inputClearDissolve
          onkeydown={handleSearchKeydown}
        />
        {#if query}
          <IconButton class="clear-search" label="Clear search" onclick={clearSearch}>
            <X size={13} aria-hidden="true" />
          </IconButton>
        {/if}
      </div>
    </div>
  {/if}

  <div class="items" tabindex="-1" bind:this={itemsElement} onscroll={follower.handleScroll} {@attach turnFocus}>
    {#if groups.length === 0}
      <EmptyState title="Step through the stream to build the logs." icon={ScrollText} />
    {:else if visibleGroups.length === 0}
      <EmptyState title="No events match this filter." icon={ListFilter} />
    {:else}
      <!-- Keyed, because a scrub inserts into the MIDDLE of this list rather than only
           appending to it. Rows arrive in frame order but render grouped by turn, and
           the two orders disagree: a queued message is accepted for a turn that has not
           started, so its row sits here while the turn before it is still filling in
           above. On the mock run 23 of 162 ticks move a row that is already on screen,
           63 of them at once. Unkeyed, Svelte rewrites all 63 in place. -->
      <div class="turns" role="feed" aria-label="Turns" style:height={`${$turns.getTotalSize()}px`}>
      {#each $turns.getVirtualItems() as item (item.key)}
        {@const group = visibleGroups[item.index]}
        {#if group}
        {@const summary = group.summary}
        {@const preview = markdownToText(summary.preview)}
        {@const figures = turnFigures(summary)}
        {@const events = plural(summary.eventCount, "event")}
        <section
          class={`turn-group ${activeTurnNumber === group.turnNumber ? "active-turn" : ""}`}
          role="article"
          aria-label={`Turn ${group.turnNumber}`}
          aria-posinset={item.index + 1}
          aria-setsize={visibleGroups.length}
          tabindex="-1"
          data-index={item.index}
          style:top={`${item.start}px`}
          {@attach measureTurn}
        >
          <header class="turn-head">
            <div class="turn-line">
              <span class="turn-title">Turn {group.turnNumber}</span>
              {#if Math.round(summary.durationSeconds) > 0}
                <span class="turn-duration" data-tip={`Took ${formatDuration(summary.durationSeconds)} · ${events}`}>
                  {formatDuration(summary.durationSeconds)}
                </span>
              {/if}
              {#if Number.isFinite(group.startedAt)}
                <time
                  class="turn-time"
                  datetime={new Date(group.startedAt * 1000).toISOString()}
                  data-tip={formatTimestamp(group.startedAt, FULL_TIME)}
                >
                  {formatTimestamp(group.startedAt, SHORT_TIME)}
                </time>
              {/if}
            </div>
            <!-- The tip sits on a box that does not clip, or the fade would clip it too. -->
            <div class="turn-preview" data-tip={preview}>
              <span class="fade-end" dir="auto">{preview}</span>
            </div>
            {#if figures.length}
              <div class="turn-stats" role="group" aria-label={`Turn ${group.turnNumber} summary, ${events}`}>
                {#each figures as figure (figure.key)}
                  {@const Icon = figureIcon(figure.key)}
                  <span class={["turn-figure", figure.key]} role="img" aria-label={figure.words} data-tip={figure.words}>
                    {#if Icon}<Icon size={13} aria-hidden="true" />{/if}
                    <span class="fade-end">{figure.value}</span>
                  </span>
                {/each}
              </div>
            {/if}
          </header>

          <div class="log-lines" id={`turn-${group.turnNumber}-logs`} style:--offset-ch={cachedLines(group).offsetWidth}>
            <WindowedRows
              items={linesFor(group)}
              getKey={(line) => line.id}
              scroller={itemsElement}
              start={item.start}
              estimateSize={LINE_HEIGHT}
              bind:this={rowLists[group.turnNumber]}
            >
              {#snippet children(line)}
              {@const active = holdsActive(line)}
              <!-- Signal is drawn bright beside its glyph; lifecycle is drawn dim, so the eye
                   lands on the calls, and on what went wrong or is waiting, without the rest
                   going missing. -->
              {@const tone = lineTone(line, active)}
              {@const offset = cachedLines(group).offsets.get(line.id) ?? ""}
              {#if line.kind === "step"}
                {@const step = line.step}
                {@const cells = stepCells(step)}
                <!-- Folded, the call's bar runs on through its trail, so the two read as one line. -->
                <article
                  id={lineElementId(line)}
                  class={["log-line", "log-step", tone, active && "active-row"]}
                  style:--hue={step.state === "failed" || step.state === "rejected" ? "var(--error)" : actorHue(step.rows[0]!.actor)}
                >
                  <TraceRow
                    glyph={STEP_GLYPHS[step.state]}
                    name={step.name}
                    mono={step.kind === "tool" || step.kind === "model"}
                    status={step.status}
                    figure={cells.figure}
                    note={cells.note}
                    meta={offset}
                    tip={cells.tip}
                    depth={line.depth}
                    expandable
                    expanded={!line.collapsed}
                    onToggle={() => toggleStep(line)}
                    media={line.collapsed ? trail : undefined}
                  />
                  {#snippet trail()}
                    {@const runs = eventRuns(step.rows)}
                    <div class="trail-line" style:--depth={line.depth > 0 ? line.depth : undefined}>
                      <span class="trail-offset" aria-hidden="true"></span>
                      <span class="trail-glyph" aria-hidden="true"></span>
                      <div class="trail" role="group" aria-label={`${step.name} events`}>
                        {#each runs as run, index (run.row.id)}
                          {@const Icon = actorIcon(run.row.actor)}
                          {#if index > 0}<span class="trail-link" aria-hidden="true"></span>{/if}
                          <button
                            type="button"
                            class="trail-event"
                            style:color={trailHue(run.row)}
                            aria-label={run.count > 1 ? `${run.row.label}, ${run.count} in a row` : run.row.label}
                            data-tip={eventWords(run.row)}
                            onclick={() => revealRow(group, step, run.row.id)}
                          >
                            <Icon size={12} aria-hidden="true" />
                            {#if run.count > 1}<span class="trail-count">×{run.count}</span>{/if}
                          </button>
                        {/each}
                      </div>
                    </div>
                  {/snippet}
                </article>
              {:else}
              {@const row = line.row}
              <!-- A seam, drawn where it is rather than as a badge on the rows around
                   it: the log's whole claim is that consecutive lines are consecutive
                   events, and this is the one place that is untrue. -->
              {#if row.gapBefore}
                <p class="history-gap">{row.gapBefore}</p>
              {/if}
              {@const expanded = isRowExpanded(row.id)}
              {@const status = statusNote(row)}
              {@const glyph = rowGlyph(row)}
              {@const RowIcon = actorIcon(row.actor)}
              <article
                id={lineElementId(line)}
                class={["log-line", tone, active && "active-row", revealedRowId === row.id && "revealed"]}
                style:--hue={rowHue(row)}
              >
                <!-- The protocol's own name for the event, so a reader learns the event types by
                     reading the log; the same thing in a sentence is in the tip. -->
                <TraceRow
                  {glyph}
                  glyphLabel={glyph && status ? status : undefined}
                  name={line.underStep ? undefined : rowName(row)}
                  mono
                  figure={rowFigure(row)}
                  note={rowNote(row, glyph, status)}
                  meta={offset}
                  tip={[formatTimestamp(row.timestamp), row.label, row.sourceLabel].filter(Boolean).join(" · ")}
                  depth={line.depth}
                  expandable={rowHasDetails(row) || row.citations.length > 0}
                  {expanded}
                  controls={`log-row-${row.id}-details`}
                  onToggle={() => toggleRow(row.id)}
                >
                    {@const primary = primaryPayload(row)}
                    {@const fullDetail = fullLogDetail(row)}
                    {@const ids = rowIdentifiers(row)}
                    {@const images = toolImages(row.output, row.toolName)}
                    {@const call = cachedLines(group).stepOf.get(row.id)}
                    <div class="line-details" id={`log-row-${row.id}-details`}>
                      <!-- The rest of the call this event belongs to, so an event reads in its place
                           without scrolling back up to the head. -->
                      {#if call}
                        <section class="call-events" aria-label={`${call.name} events`}>
                          <span class="payload-label">
                            {call.name}{#if call.durationMs}<span class="call-took"> · took {formatElapsedDuration(call.durationMs)}</span>{/if}
                          </span>
                          <ol>
                            {#each eventRuns(call.rows) as run (run.row.id)}
                              {@const Icon = actorIcon(run.row.actor)}
                              {@const here = run.ids.includes(row.id)}
                              <li>
                                <button type="button" class:here aria-current={here ? "true" : undefined} onclick={() => revealRow(group, call, here ? row.id : run.row.id)}>
                                  <span class="call-icon" style:color={trailHue(run.row)}><Icon size={12} aria-hidden="true" /></span>
                                  <span class="call-type">{eventWords(run.row)}{#if run.count > 1}<span class="call-count">×{run.count}</span>{/if}</span>
                                  <span class="call-at">+{formatDuration(run.row.timestamp - call.startedAt)}</span>
                                </button>
                              </li>
                            {/each}
                          </ol>
                        </section>
                      {/if}
                      <TraceImages {images} />
                      {#if primary}
                        <section class="primary-payload" aria-label={`${primary.label} preview`}>
                          <span class="payload-label">{primary.label}</span>
                          <LongContent id={`log-${row.id}-${primary.label}`} text={primary.text} label={`${primary.label} preview`} onResize={() => remeasureLogRow(group.turnNumber)}>
                            <Copyable block value={primary.text} label={`Copy ${primary.label}`}>
                              <pre class="primary-pre">{primary.text}</pre>
                            </Copyable>
                          </LongContent>
                        </section>
                      {/if}
                      {#if row.citations.length}
                        <div class="citations">
                          {#each citationLinks(row.citations) as link (link.number)}
                            <a href={link.url} target="_blank" rel="noreferrer" aria-label={citationAriaLabel(link)}>{link.title}</a>
                          {/each}
                        </div>
                      {/if}
                      {#if ids.length}
                        <dl class="row-ids">
                          {#each ids as id (id.label)}
                            <div>
                              <dt>{id.label}</dt>
                              <dd>
                                <Copyable value={id.value} label={`Copy ${id.label} ID`}>
                                  <code class="fade-end" title={id.value}>{id.value}</code>
                                </Copyable>
                              </dd>
                            </div>
                          {/each}
                        </dl>
                      {/if}
                      <section class="full-details" aria-label="Full details">
                        <LongContent id={`log-${row.id}-details`} text={fullDetail} label="Full details" onResize={() => remeasureLogRow(group.turnNumber)}>
                          <Copyable block value={fullDetail} label="Copy full details">
                            <pre>{fullDetail}</pre>
                          </Copyable>
                        </LongContent>
                      </section>
                    </div>
                    {#snippet badge()}
                      <Chip size="xs" tone={rowTone(row)} fill="quiet" toned dense class="event-type" label={eventWords(row)}>
                        {#snippet lead()}<RowIcon size={13} />{/snippet}
                      </Chip>
                    {/snippet}
                </TraceRow>
              </article>
              {/if}
              {/snippet}
            </WindowedRows>
          </div>
        </section>
        {/if}
      {/each}
      </div>
    {/if}
  </div>
</section>

<style>
  .transcript {
    min-width: 330px;
    max-width: 430px;
    border-left: 1px solid var(--border);
    background: var(--surface-1);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* The chips give way first; the count only drops to its own line when even the
     widest chip beside it would cut it short. */
  .filter-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
  }

  .filter-row .filter-chips {
    flex: 1 1 0;
  }

  /* A figure, not a control, so no box: the turn header's figures read the same way. */
  .event-count {
    line-height: var(--control-height-xs);
    color: var(--text-3);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .transcript-controls {
    display: grid;
    gap: 8px;
    padding: var(--pane-inset);
    border-bottom: 1px solid var(--border);
  }

  .filter-chips {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 4px;
  }

  /* A row you type into, so the lg box: the same token and the same 34px as the
     session search in SessionControls. The height lives on the box rather than
     on the input, which had been sitting on a literal 30px. */
  .items {
    min-height: 0;
    overflow-y: auto;
    scrollbar-gutter: stable;
    padding: var(--pane-inset);
    padding-inline-end: calc(var(--pane-inset) - var(--scrollbar-size));
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .turns {
    position: relative;
    flex: 0 0 auto;
  }

  /* The pane scroller clips at its edge, so the ring goes inside. */
  .turn-group:focus-visible {
    outline-offset: -2px;
  }

  /* A turn is not something to press, so no card: a section ruled off from the next, as the
     waterfall's turn rows are. */
  .turn-group {
    position: absolute;
    inset-inline: 0;
    display: flex;
    flex-direction: column;
    padding-block-end: var(--gap-md);
    border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  }

  /* Three lines at every width, none of them wrapping: the turn, its time and how long it took;
     the message, faded at its end; the counts. Every word that only repeats what an icon or
     the rows below already say is in a tip. Inset like a row's glyph, so the turn's words and
     its rows' glyphs start at one x. */
  .turn-head {
    display: grid;
    gap: 2px;
    padding: 6px var(--gap-md) 8px;
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 500;
    /* Held at the top while its turn's rows scroll under it, so a long turn never loses which
       turn it is. Opaque, since the rows pass beneath. */
    position: sticky;
    top: calc(-1 * var(--pane-inset));
    z-index: 1;
    background: var(--surface-1);
  }

  /* The turn holding the cursor, by its header; the cursor's own row carries the same tint. */
  .turn-group.active-turn .turn-head {
    background: linear-gradient(var(--selected-bg), var(--selected-bg)) var(--surface-1);
  }

  .turn-line {
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: 8px;
    white-space: nowrap;
  }

  .turn-title {
    min-width: 0;
    overflow: hidden;
    color: var(--text-1);
    font-size: var(--font-md);
    font-weight: 650;
  }

  .turn-duration,
  .turn-time {
    flex: none;
    font-variant-numeric: tabular-nums;
  }

  .turn-time {
    margin-inline-start: auto;
  }

  .turn-preview {
    min-width: 0;
    display: flex;
  }

  .turn-preview > span {
    min-width: 0;
  }

  /* Figures nobody can press, so icons and numbers in a line rather than boxes. The counts never
     give way; the token total, last, fades instead of wrapping. */
  .turn-stats {
    min-width: 0;
    display: flex;
    flex-wrap: nowrap;
    align-items: center;
    gap: 12px;
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  /* A number in any language, so isolated left to right beside its icon. */
  .turn-figure {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    direction: ltr;
    unicode-bidi: isolate;
  }

  .turn-figure.tokens {
    flex: 0 1 auto;
    min-width: 0;
  }

  .turn-figure > span {
    min-width: 0;
  }

  .turn-figure.model :global(svg) { color: var(--model); }
  .turn-figure.tool :global(svg) { color: var(--tool); }
  .turn-figure.approval :global(svg) { color: var(--live); }
  .turn-figure.error { color: var(--error); }

  /* Each line's offset into its turn leads the row, in a column as wide as the turn's longest
     offset (--offset-ch), so a short row ends early instead of reaching for a time at the far
     edge. Mono, so a character is the same width the column was counted in. */
  .log-line :global(.trace-meta) {
    order: -1;
    width: calc(var(--offset-ch, 4) * 1ch + var(--gap-md));
    min-width: 0;
    padding-inline-start: var(--gap-md);
    font-family: var(--font-mono);
    text-align: start;
    white-space: nowrap;
  }

  /* Then the event's bar, in who it is from: one hue per kind of event, down the whole row,
     painted on the glyph's cell (a row's ::before is its tip). A nested event steps its bar in,
     not the offset, so the column stays straight. */
  .log-line :global(.trace-glyph),
  .trail-glyph {
    box-sizing: border-box;
    align-self: stretch;
    width: calc(15px + 11px);
    height: auto;
    margin-inline-start: calc(1px + var(--depth, 0) * 14px);
    padding-inline-start: 11px;
    background: linear-gradient(var(--hue, var(--text-4)), var(--hue, var(--text-4))) no-repeat 0 0 / 3px 100%;
  }

  .log-line :global(.trace-row.nested > .trace-line) {
    width: 100%;
    margin-inline-start: 0;
  }

  /* The type is what the row is for, so it keeps its width and the tool's name gives way. */
  .log-line :global(.event-type) {
    flex: none;
    align-self: center;
    height: 18px;
    gap: 3px;
    padding: 0 5px;
    font-size: var(--font-xs);
    letter-spacing: 0.03em;
  }

  .log-line.routine :global(.event-type) {
    opacity: 0.72;
  }

  .log-lines {
    display: grid;
    overflow: visible;
  }

  /* Same tone and the same words as the waterfall's seam, so the two surfaces are
     read as one fact about the run rather than two coincidences. */
  .history-gap {
    margin: 0;
    padding: 7px var(--gap-md);
    border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
    background: color-mix(in srgb, var(--error) 8%, transparent);
    color: var(--text-2);
    font-size: var(--font-xs);
    line-height: 1.45;
  }

  .log-line.active-row {
    background: var(--selected-bg);
  }

  .log-line.revealed :global(.trace-line) {
    background: var(--control-hover);
  }

  .log-line.signal :global(strong) {
    color: var(--text-1);
  }

  .log-line.routine :global(strong) {
    color: var(--text-3);
  }

  .log-line.routine :global(:is(.trace-note, .trace-meta)) {
    color: var(--text-4);
  }

  /* The chat trace's row at the log's density: the whole row is the button, at the
     LINE_HEIGHT WindowedRows estimates for it, with no rule between rows and no padding above
     or below, so one row's bar meets the next. The pane scroller clips, so the focus ring goes
     inside the row. */
  .log-line :global(.trace-row) {
    gap: 0;
  }

  .log-line :global(.trace-line) {
    grid-template-columns: auto auto minmax(0, 1fr);
    min-height: var(--control-height);
    padding: 0 var(--gap-md) 0 0;
    border-radius: 0;
  }

  .log-line :global(.trace-line:focus-visible) {
    outline-offset: -2px;
  }

  /* A folded call's events, in order, under its name: one icon per event, a run of the same
     event once with its count. Each opens that event. Laid out on the row's own columns, so the
     call's bar runs straight on through it. */
  .trail-line {
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr);
    gap: 8px;
    padding-inline-end: var(--gap-md);
  }

  .trail-offset {
    width: calc(var(--offset-ch, 4) * 1ch + var(--gap-md));
    font-family: var(--font-mono);
    font-size: var(--font-sm);
  }

  .trail {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    row-gap: 4px;
    padding-block-end: 6px;
  }

  .trail-event {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 0;
    border: 0;
    background: none;
    font: inherit;
    cursor: pointer;
  }

  .trail-event:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  .trail-count {
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
  }

  .trail-link {
    width: 8px;
    height: 1px;
    background: var(--border-strong);
  }

  /* An open event's call: each of its events by the protocol's name, this one marked, and how
     long after the call began. */
  .call-events {
    min-width: 0;
    display: grid;
    gap: 5px;
  }

  .call-took {
    color: var(--text-3);
    font-weight: 400;
  }

  .call-events ol {
    display: grid;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .call-events button {
    width: 100%;
    min-height: 22px;
    display: flex;
    gap: 8px;
    align-items: center;
    padding: 0 6px;
    border: 0;
    background: none;
    color: var(--text-3);
    font: var(--font-sm) var(--font-mono);
    text-align: start;
    cursor: pointer;
  }

  .call-events button:hover {
    background: var(--control-hover);
  }

  .call-events button.here {
    background: var(--selected-bg);
    color: var(--text-1);
  }

  .call-events button:focus-visible {
    outline: 1px solid var(--focus-ring);
    outline-offset: -1px;
  }

  .call-icon {
    display: inline-flex;
  }

  .call-type {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  .call-count {
    margin-inline-start: 6px;
    color: var(--text-4);
    font-variant-numeric: tabular-nums;
  }

  .call-at {
    color: var(--text-4);
    font-variant-numeric: tabular-nums;
  }

  /* At the turn header's inset whatever the row's depth (TraceRow steps in only its line), so a
     deep row's details get the turn's full width. */
  .line-details {
    display: grid;
    gap: 8px;
    padding: 0 var(--gap-md) var(--gap-md);
  }

  p {
    margin: 0;
    color: var(--text-1);
    font-size: var(--font-md);
    line-height: 1.42;
    word-break: break-word;
  }

  .primary-payload {
    min-width: 0;
    display: grid;
    gap: 5px;
  }

  .payload-label {
    color: var(--text-3);
    font-size: var(--font-sm);
    font-weight: 650;
  }

  /* Capped like CallInput's full input, so a traceback scrolls inside its block instead of
     filling the pane. */
  pre {
    margin: 0;
    padding: 8px;
    border-radius: var(--radius-sm);
    overflow: auto;
    background: var(--surface-0);
    color: var(--text-2);
    font-size: var(--font-sm);
  }

  .primary-pre {
    color: var(--text-1);
  }

  /* One label column for the whole list, as wide as its widest label, so the
     values line up whichever IDs a row happens to have. */
  .row-ids {
    display: grid;
    grid-template-columns: max-content minmax(0, 1fr);
    gap: 2px 6px;
    margin: 0;
  }

  .row-ids div {
    min-width: 0;
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: subgrid;
    align-items: center;
  }

  .row-ids dt {
    color: var(--text-3);
    font-family: var(--font-mono);
    font-size: var(--font-2xs);
    text-transform: uppercase;
  }

  .row-ids dd {
    min-width: 0;
    margin: 0;
  }

  .row-ids code {
    min-width: 0;
    color: var(--text-2);
    font-family: var(--font-mono);
    font-size: var(--font-sm);
  }

  .full-details pre {
    color: var(--text-2);
    /* A bare `pre` clips rather than scrolls in a pane this narrow, so anything past the
       fold was silently gone. AgentChatPanel's equivalent block already wraps; this one
       only got away with it while every value here was short. */
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .citations {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .citations a {
    color: var(--accent);
    font-size: var(--font-sm);
    text-decoration: none;
    border-bottom: 1px solid color-mix(in srgb, var(--accent) 50%, transparent);
  }
</style>
