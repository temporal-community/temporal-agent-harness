<script lang="ts" module>
  import {
    Bot,
    ChevronLeft,
    ChevronRight,
    Cpu,
    MessageCircleQuestion,
    ShieldAlert,
    ShieldCheck,
    Wrench,
    XCircle
  } from "@lucide/svelte";
  import type { WorkflowTimelineRow } from "$lib/state/stepTimeline";

  /** The glyph that says what a row is, the same one the waterfall, approvals and pending
      banners draw for it. */
  export function rowIcon(row: Pick<WorkflowTimelineRow, "kind" | "waitType">): typeof Cpu {
    switch (row.kind) {
      case "model":
        return Cpu;
      case "tool":
        return Wrench;
      case "approval":
        return row.waitType === "callback" ? MessageCircleQuestion : ShieldAlert;
      case "evaluation":
        return ShieldCheck;
      case "subagent":
        return Bot;
      default: {
        const unhandled: never = row.kind;
        throw new Error(`No icon for timeline row kind ${String(unhandled)}`);
      }
    }
  }

  /** What a row is, for its tip. */
  export function rowKindLabel(row: Pick<WorkflowTimelineRow, "kind" | "waitType">): string {
    switch (row.kind) {
      case "model":
        return "Model call";
      case "tool":
        return "Tool call";
      case "approval":
        return row.waitType === "callback" ? "Callback" : "Approval";
      case "evaluation":
        return "Approval check";
      case "subagent":
        return "Subagent";
      default: {
        const unhandled: never = row.kind;
        throw new Error(`No label for timeline row kind ${String(unhandled)}`);
      }
    }
  }

  /** Whether a row is time spent waiting on a person (an approval gate or a callback the
      client answers) rather than work, so its lane is drawn as waiting. */
  export function waitsOnPerson(row: Pick<WorkflowTimelineRow, "kind">): boolean {
    switch (row.kind) {
      case "approval":
        return true;
      case "model":
      case "tool":
      case "evaluation":
      case "subagent":
        return false;
      default: {
        const unhandled: never = row.kind;
        throw new Error(`Unknown timeline row kind ${String(unhandled)}`);
      }
    }
  }

  export type EdgeSide = "before" | "after";

  /** In a narrow pane, the side of the plot a row's start cap at `x` has scrolled or zoomed
      past, or null while any of the cap is in view. With no label column such a row would be a
      blank line, so it is named at that edge instead. */
  export function edgeSide(x: number, scrollLeft: number, viewportWidth: number, capHalf: number): EdgeSide | null {
    if (x < scrollLeft - capHalf) return "before";
    if (x > scrollLeft + viewportWidth + capHalf) return "after";
    return null;
  }

  /** The label column fits a caret, a few letters and a duration at its narrowest, and gives
      the plot the rest past 220px or 30% of the pane; a longer name fades out at its end. */
  export const LABEL_WIDTH = { min: 120, max: 220, share: 0.3 };

  export interface LabelMetrics {
    /** A string's width in the row label's font. */
    measure: (text: string) => number;
    padding: number;
    gap: number;
    caret: number;
    indentStep: number;
    indentCap: number;
  }

  export interface LabelRow {
    label: string;
    depth: number;
    duration: string;
    count?: number;
  }

  /** The widest row's label cell at its natural width: padding, indent, caret cell, name, fold
      count and duration, a gap between each. Digits are measured as zeros, the tabular width in
      most faces, so a ticking duration does not change it. */
  export function labelContentWidth(rows: readonly LabelRow[], carets: boolean, metrics: LabelMetrics): number {
    const tabular = (text: string) => metrics.measure(text.replace(/\d/g, "0"));
    return rows.reduce((widest, row) => {
      const parts = [metrics.measure(row.label), tabular(row.duration)];
      if (carets) parts.push(metrics.caret);
      if (row.count) parts.push(tabular(`+${row.count}`));
      const width =
        2 * metrics.padding +
        Math.min(row.depth, metrics.indentCap) * metrics.indentStep +
        parts.reduce((sum, part) => sum + part, 0) +
        (parts.length - 1) * metrics.gap;
      return Math.max(widest, width);
    }, 0);
  }

  export function labelColumnWidth(content: number, plotWidth: number): number {
    const max = Math.min(LABEL_WIDTH.max, LABEL_WIDTH.share * plotWidth);
    return Math.ceil(Math.max(LABEL_WIDTH.min, Math.min(max, content)));
  }
</script>

<script lang="ts">
  import { onDestroy, tick, untrack } from "svelte";
  import type { Attachment } from "svelte/attachments";
  import { createSubscriber, SvelteMap, SvelteSet } from "svelte/reactivity";
  import EmptyState from "$lib/components/primitives/EmptyState.svelte";
  import TipCard from "$lib/components/primitives/TipCard.svelte";
  import { placeTip } from "$lib/components/primitives/tipFocus";
  import WindowedRows from "$lib/components/primitives/WindowedRows.svelte";
  import { windowedFocus } from "$lib/components/primitives/windowedFocus";
  import { formatDuration, formatElapsedDuration, type ReplayMarker } from "$lib/state/replayLog";
  import {
    indexAtTimestamp,
    isSignalRow,
    liveIdleGaps,
    shownWorkflowRows,
    timestampAtIndex,
    type StepTimeline
  } from "$lib/state/stepTimeline";
  import {
    anchoredZoomScroll,
    buildWorkflowScale,
    distinctTickLabels,
    followPlayheadScroll,
    GAP_BREAK_PATH,
    inlineLabelBox,
    projectTime,
    tickLabelBoxes,
    unprojectTime,
    visibleTimeRange,
    visibleTimelineSpans,
    workflowScaleTicks,
    type CollapsedGap
  } from "$lib/state/workflowTimelineScale";
  import type { MarkGeometry } from "./timelineMark";
  import { playheadAxisX } from "./usageChart";

  interface Props {
    timeline: StepTimeline;
    viewIndex: number;
    anomalyMarkers?: ReplayMarker[];
    /** The session is still open, so a span still open runs on to now wherever the cursor is. */
    live?: boolean;
    onScrub: (index: number) => void;
  }

  let { timeline, viewIndex, anomalyMarkers = [], live = false, onScrub }: Props = $props();

  const playheadTimestamp = $derived(timestampAtIndex(timeline.workflow.points, viewIndex));
  /** The reader's own folds, which win over the default fold, read at the cursor. */
  const folds = new SvelteMap<string, boolean>();
  const tree = $derived(shownWorkflowRows(timeline.workflow, folds, playheadTimestamp ?? undefined));
  /** The deepest shown row's depth, which a narrow pane's gutter makes room for. */
  const shownDepth = $derived(tree.indexes.reduce((deepest, index) => Math.max(deepest, rowAt(index)!.depth), 0));
  /** Whether any row has a parent, so a leaf keeps the caret's cell and its name lines up. */
  const nested = $derived(shownDepth > 0 || Object.keys(tree.hidden).length > 0);
  let windowedRows = $state<{ scrollToIndex: (index: number) => void } | null>(null);
  let focusedKey = $state<string | null>(null);
  const startTs = $derived(timeline.workflow.bounds.startTs);

  /* Frames carry the server's clock. Wall time is trusted when it lands within a day after
     the newest frame; past that (a skewed clock, recorded mock history) the open span grows
     from the newest frame by the time since it was first seen, which undercounts a wait
     that began before a reload. */
  const CLOCK_TRUST_SECONDS = 86_400;
  const subscribeClock = createSubscriber((update) => {
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  });
  let anchor = { endTs: Number.NaN, wallSeconds: 0 };
  const nowTs = $derived.by(() => {
    if (!live || !timeline.workflow.ongoing) return null;
    subscribeClock();
    const wallSeconds = Date.now() / 1000;
    const newest = timeline.workflow.bounds.endTs;
    const ahead = wallSeconds - newest;
    if (ahead >= 0 && ahead <= CLOCK_TRUST_SECONDS) return wallSeconds;
    if (anchor.endTs !== newest) anchor = { endTs: newest, wallSeconds };
    return newest + Math.max(0, wallSeconds - anchor.wallSeconds);
  });
  const endTs = $derived(
    Math.max(startTs, timeline.workflow.bounds.endTs, nowTs ?? Number.NEGATIVE_INFINITY)
  );
  const duration = $derived(Math.max(0, endTs - startTs));

  const MIN_ZOOM = 1;
  const MAX_ZOOM = 32;
  const ZOOM_STEP = Math.SQRT2;
  const HORIZONTAL_OVERSCAN = 96;

  let rowsElement = $state<HTMLElement | null>(null);
  const rowsNode: Attachment<HTMLElement> = (node) => {
    rowsElement = node;
    return () => {
      if (rowsElement === node) rowsElement = null;
    };
  };
  let rootElement = $state<HTMLElement | null>(null);
  let plotElement = $state<HTMLElement | null>(null);
  let plotWidth = $state(0);
  let labelWidth = $state(0);
  let scrollLeft = $state(0);
  let zoom = $state(1);
  let expandedGapKeys = $state<string[]>([]);
  const viewportWidth = $derived(Math.max(0, (plotWidth || 600) - labelWidth));
  const contentWidth = $derived(viewportWidth * zoom);
  const gaps = $derived(liveIdleGaps(timeline.workflow.gaps, timeline.workflow.bounds, nowTs));
  const collapsedGaps = $derived(gaps.filter((gap) => !expandedGapKeys.includes(gap.key)));
  const scale = $derived(
    buildWorkflowScale({ startTs, endTs }, collapsedGaps, contentWidth)
  );
  const tickLabel = (tick: number) => formatDuration(tick - startTs);
  const ticks = $derived(distinctTickLabels(workflowScaleTicks(scale), tickLabel));
  const visibleRange = $derived(
    visibleTimeRange(scale, scrollLeft, viewportWidth, HORIZONTAL_OVERSCAN)
  );
  /** A tick label's width in the axis font, digits as zeros like the label column's; before
      the pane is measured, 6px a character, a little wider than the font. */
  const tickWidth = $derived.by(() => {
    measuringContext ??= rootElement && document.createElement("canvas").getContext("2d");
    const context = measuringContext;
    if (!rootElement || !context) return (tick: number) => tickLabel(tick).length * 6;
    const root = getComputedStyle(rootElement);
    const font = `${root.fontWeight} ${root.getPropertyValue("--font-xs")} ${root.fontFamily}`;
    return (tick: number) => {
      context.font = font;
      return context.measureText(tickLabel(tick).replace(/\d/g, "0")).width;
    };
  });
  const tickBoxes = $derived(tickLabelBoxes(scale, ticks, tickWidth));
  /* A tick under the sticky head cell would show only its tail, which reads as a different
     time: "3h 13m 00s" cut to "0s". */
  const visibleTicks = $derived(
    tickBoxes.filter(
      (box) =>
        box.tick >= visibleRange[0] &&
        box.tick <= visibleRange[1] &&
        (box.tick === ticks[0] || box.left >= scrollLeft)
    )
  );

  /** Where a span still open is drawn to: now while live, the cursor in replay. */
  const openEndTs = $derived(nowTs ?? playheadTimestamp);

  function spanEnd(row: WorkflowTimelineRow): number {
    return row.ongoing && openEndTs != null ? Math.max(row.endTs, openEndTs) : row.endTs;
  }
  const playheadX = $derived(
    playheadTimestamp == null ? null : playheadAxisX(scale, playheadTimestamp, playheadTimestamp)
  );
  const cues = $derived(
    anomalyMarkers.flatMap((marker) => {
      const timestamp = timestampAtIndex(timeline.workflow.points, marker.index);
      return timestamp == null ? [] : [{ marker, startTs: timestamp, endTs: timestamp }];
    }).toSorted((a, b) => a.startTs - b.startTs)
  );
  const visibleCues = $derived(
    visibleTimelineSpans(cues, visibleRange[0], visibleRange[1]).map((cue) => ({
      ...cue,
      x: projectTime(scale, cue.startTs)
    }))
  );
  const visibleGaps = $derived(
    collapsedGaps.filter((gap) => gap.endTs >= visibleRange[0] && gap.startTs <= visibleRange[1])
  );
  const visibleGapControls = $derived(
    gaps.filter((gap) => gap.endTs >= visibleRange[0] && gap.startTs <= visibleRange[1])
  );

  let scrubGesture: {
    element: HTMLElement;
    pointerId: number;
    left: number;
    width: number;
    rtl: boolean;
  } | null = null;
  let scrubFrame = 0;
  let pendingClientX: number | null = null;
  let latestScrollLeft = 0;
  let userScrolling = false;
  let userScrollTimer = 0;
  let programmaticScroll = false;
  let suppressFollowOnce = false;
  let scrollbarGesture = false;

  const rootNode: Attachment<HTMLElement> = (node) => {
    rootElement = node;
    return () => {
      if (rootElement === node) rootElement = null;
    };
  };

  const plotNode: Attachment<HTMLElement> = (node) => {
    plotElement = node;
    return () => {
      if (plotElement === node) plotElement = null;
    };
  };

  const followPlayhead: Attachment<HTMLElement> = (node) => {
    const x = playheadX;
    if (x == null) return;
    if (suppressFollowOnce) {
      suppressFollowOnce = false;
      return;
    }
    const next = followPlayheadScroll(
      x,
      latestScrollLeft,
      viewportWidth,
      contentWidth,
      scrubGesture != null || scrollbarGesture || userScrolling
    );
    if (next === node.scrollLeft) return;
    programmaticScroll = true;
    latestScrollLeft = next;
    node.scrollTo({ left: next });
  };

  function scrubAt(clientX: number): void {
    if (!scrubGesture || scrubGesture.width === 0) return;
    let x = clientX - scrubGesture.left;
    if (scrubGesture.rtl) x = scrubGesture.width - x;
    const index = indexAtTimestamp(timeline.workflow.points, unprojectTime(scale, x));
    if (index != null) onScrub(index);
  }

  function startScrub(event: PointerEvent): void {
    if (event.button !== 0 || !event.isPrimary) return;
    event.preventDefault();
    const element = event.currentTarget as HTMLElement;
    const box = element.getBoundingClientRect();
    const horizontallyWindowed = element.classList.contains("track");
    element.setPointerCapture(event.pointerId);
    scrubGesture = {
      element,
      pointerId: event.pointerId,
      left: box.left - (horizontallyWindowed ? scrollLeft : 0),
      width: horizontallyWindowed ? contentWidth : box.width,
      rtl: getComputedStyle(element).direction === "rtl"
    };
    scrubAt(event.clientX);
  }

  function moveScrub(event: PointerEvent): void {
    if (scrubGesture?.pointerId !== event.pointerId) return;
    pendingClientX = event.clientX;
    if (scrubFrame) return;
    scrubFrame = requestAnimationFrame(() => {
      scrubFrame = 0;
      if (pendingClientX != null) scrubAt(pendingClientX);
      pendingClientX = null;
    });
  }

  function stopScrub(event: PointerEvent): void {
    if (scrubGesture?.pointerId !== event.pointerId) return;
    if (scrubGesture.element.hasPointerCapture(event.pointerId)) {
      scrubGesture.element.releasePointerCapture(event.pointerId);
    }
    scrubGesture = null;
    pendingClientX = null;
    if (scrubFrame) cancelAnimationFrame(scrubFrame);
    scrubFrame = 0;
  }

  onDestroy(() => {
    if (scrubFrame) cancelAnimationFrame(scrubFrame);
    if (userScrollTimer) window.clearTimeout(userScrollTimer);
  });

  function rowAt(index: number): WorkflowTimelineRow | undefined {
    return timeline.workflow.rowAt(index);
  }

  /** Half of `--cap`. */
  const CAP_HALF = 11;

  function geometry(row: WorkflowTimelineRow): MarkGeometry & { showStart: boolean; showEnd: boolean } {
    const startPx = projectTime(scale, row.startTs);
    const endPx = projectTime(scale, spanEnd(row));
    const visibleStartPx = Math.max(0, scrollLeft - HORIZONTAL_OVERSCAN);
    const visibleEndPx = Math.min(contentWidth, scrollLeft + viewportWidth + HORIZONTAL_OVERSCAN);
    const clippedStart = Math.max(startPx, visibleStartPx);
    const clippedEnd = Math.min(endPx, visibleEndPx);
    /* Narrower than two caps, a start and an end cap would overlap with a sliver of line
       between: one cap stands for the whole span. */
    const point = endPx - startPx < 4 * CAP_HALF;
    const onScreen = (px: number) => px >= visibleStartPx && px <= visibleEndPx;
    return {
      leftPct: contentWidth > 0 ? (clippedStart / contentWidth) * 100 : 0,
      widthPct: contentWidth > 0 ? ((clippedEnd - clippedStart) / contentWidth) * 100 : 0,
      startPx: clippedStart,
      endPx: clippedEnd,
      point,
      showStart: !point && onScreen(startPx),
      showEnd: !point && onScreen(endPx)
    };
  }

  /** The container query's breakpoint, under which the pane drops its label column and names
      each span on the plot instead. Unmeasured counts as narrow; CSS hides the names in a wide
      pane until it is measured. */
  const NARROW_PANE = 560;
  const inlineLabels = $derived(plotWidth < NARROW_PANE);
  const rtl = $derived(rootElement != null && getComputedStyle(rootElement).direction === "rtl");
  const inlineGap = $derived(
    (rootElement && Number.parseFloat(getComputedStyle(rootElement).getPropertyValue("--gap-sm"))) || 6
  );
  const breakXs = $derived(collapsedGaps.map(gapCenter));
  /** A name's width in the row label's font, measured once per name, so a live run only
      measures names it has not seen; 6px a character before the pane is measured. */
  const nameWidth = $derived.by(() => {
    measuringContext ??= rootElement && document.createElement("canvas").getContext("2d");
    const context = measuringContext;
    if (!rootElement || !context) return (text: string) => text.length * 6;
    const root = getComputedStyle(rootElement);
    const font = `${root.fontWeight} ${root.getPropertyValue("--font-xs")} ${root.fontFamily}`;
    const widths = new Map<string, number>();
    return (text: string) => {
      let width = widths.get(text);
      if (width == null) {
        context.font = font;
        width = context.measureText(text).width;
        widths.set(text, width);
      }
      return width;
    };
  });

  /** Where a narrow pane prints the row's name, from where its caps are drawn (`.cap`). Only
      a solid lane takes it: a wait's 35% tint or an open span's dashes would not hold it. */
  function inlineLabel(row: WorkflowTimelineRow) {
    const startPx = projectTime(scale, row.startTs);
    const endPx = projectTime(scale, spanEnd(row));
    const width = nameWidth(row.label);
    const shared = { plotWidth: contentWidth, breaks: breakXs, width, chars: row.label.length, gap: inlineGap, rtl };
    if (endPx - startPx < 4 * CAP_HALF) {
      const left = startPx + Math.min(CAP_HALF, contentWidth - endPx) - Math.min(CAP_HALF, startPx) - CAP_HALF;
      return inlineLabelBox({ ...shared, outer: [left, left + 2 * CAP_HALF] });
    }
    const outer = [Math.max(0, startPx - CAP_HALF), Math.min(contentWidth, endPx + CAP_HALF)] as const;
    const solid = !row.ongoing && !waitsOnPerson(row);
    return inlineLabelBox({
      ...shared,
      outer,
      lane: solid ? [outer[0] + 2 * CAP_HALF, outer[1] - 2 * CAP_HALF] : undefined
    });
  }

  /** Empty for an instant event: a point needs no duration beside its name. */
  function durationLabel(row: WorkflowTimelineRow): string {
    const seconds = spanEnd(row) - row.startTs;
    return seconds > 0 ? formatElapsedDuration(seconds * 1000) : "";
  }

  function markName(row: WorkflowTimelineRow): string {
    const duration = durationLabel(row);
    const name = duration ? `${row.label} · ${duration}` : row.label;
    if (row.ongoing) return `${name} · ongoing`;
    return row.tone === "error" ? `${name} · failed` : name;
  }

  function statusLabel(row: WorkflowTimelineRow): string {
    if (row.ongoing) return waitsOnPerson(row) ? "Waiting" : "Running";
    return row.tone === "error" ? "Failed" : "Done";
  }

  function offscreenSide(row: WorkflowTimelineRow): EdgeSide | null {
    return edgeSide(projectTime(scale, row.startTs), scrollLeft, viewportWidth, CAP_HALF);
  }

  /** A row takes keyboard focus for its tip, so the tip is placed as its row is focused. */
  function placeRowTip(row: HTMLElement): void {
    const tip = row.querySelector<HTMLElement>("[data-tip]");
    if (tip) placeTip(tip);
  }

  /** Inline style for a mark: its visible extent, plus how far a cap may hang past the
      span before the run's first or last pixel would clip it. */
  function markStyle(row: WorkflowTimelineRow, box: MarkGeometry): string {
    const origin = projectTime(scale, row.startTs);
    const endPx = projectTime(scale, spanEnd(row));
    return [
      `left: ${box.startPx}px`,
      `width: ${Math.max(0, box.endPx - box.startPx)}px`,
      `--start-room: ${origin}px`,
      `--end-room: ${Math.max(0, contentWidth - endPx)}px`
    ].join("; ");
  }

  /** Rules at the run's first and last pixel, drawn in each row as the playhead is, so they
      end at the last row rather than running down an empty pane. */
  const runEdges = $derived(
    contentWidth <= 0
      ? []
      : [
          { key: "start", px: projectTime(scale, startTs) },
          { key: "end", px: Math.max(0, projectTime(scale, endTs) - 1) }
        ]
  );

  function markClass(row: WorkflowTimelineRow): string {
    return [
      "mark",
      `kind-${row.kind}`,
      `outcome-${row.tone}`,
      row.ongoing ? "state-ongoing" : "",
      waitsOnPerson(row) ? "wait" : ""
    ].filter(Boolean).join(" ");
  }

  function gapCenter(gap: CollapsedGap): number {
    return (projectTime(scale, gap.startTs) + projectTime(scale, gap.endTs)) / 2;
  }

  function gapDuration(gap: CollapsedGap): string {
    /* A gap folds at a share of the run, so a short run folds sub-second ones: never "0s". */
    return formatElapsedDuration((gap.endTs - gap.startTs) * 1000);
  }

  function toggleGap(gap: CollapsedGap): void {
    expandedGapKeys = expandedGapKeys.includes(gap.key)
      ? expandedGapKeys.filter((key) => key !== gap.key)
      : [...expandedGapKeys, gap.key];
  }

  function isVisible(row: WorkflowTimelineRow): boolean {
    return spanEnd(row) >= visibleRange[0] && row.startTs <= visibleRange[1];
  }

  function hasChildren(index: number): boolean {
    const row = rowAt(index);
    return row != null && (rowAt(index + 1)?.depth ?? -1) > row.depth;
  }

  /** Each row's parent's index, or -1: rows are in preorder, so it is the nearest row above
      that is shallower. */
  const parents = $derived.by(() => {
    const parentOf = new Int32Array(timeline.workflow.rowCount);
    const open: number[] = [];
    for (let index = 0; index < parentOf.length; index += 1) {
      const depth = rowAt(index)!.depth;
      while (open.length > 0 && rowAt(open.at(-1)!)!.depth >= depth) open.pop();
      parentOf[index] = open.at(-1) ?? -1;
      open.push(index);
    }
    return parentOf;
  });

  /** A row's ancestors, outermost first, so each inner band paints over the one it is in. */
  function ancestorsOf(index: number): WorkflowTimelineRow[] {
    const rows: WorkflowTimelineRow[] = [];
    for (let parent = parents[index] ?? -1; parent >= 0; parent = parents[parent]!) rows.unshift(rowAt(parent)!);
    return rows;
  }

  /** The row's accessible name, with what a fold hides. */
  function rowName(row: WorkflowTimelineRow, index: number): string {
    const hidden = tree.hidden[index];
    return hidden ? `${markName(row)} · ${hidden} folded: ${tree.summaries[index]}` : markName(row);
  }

  function toggleRow(key: string): void {
    focusedKey = key;
    folds.set(key, !tree.collapsed.has(key));
  }

  /** Signal is drawn bright and routine dim, as in the Logs pane; the row under the playhead
      keeps the plain tone, so it is not lost among the dim. */
  function rowTone(row: WorkflowTimelineRow): string | undefined {
    if (isSignalRow(row)) return "signal";
    const at = playheadTimestamp;
    return at != null && row.startTs <= at && at <= spanEnd(row) ? undefined : "routine";
  }

  const tabKey = $derived(focusedKey ?? rowAt(tree.indexes[0] ?? -1)?.key);

  /** → and ← swap in a right-to-left pane, so "into" and "out of" keep their direction. */
  function logicalKey(key: string): string {
    const rtl = rowsElement != null && getComputedStyle(rowsElement).direction === "rtl";
    return rtl && key === "ArrowLeft" ? "ArrowRight" : rtl && key === "ArrowRight" ? "ArrowLeft" : key;
  }

  /** Where a key moves focus from shown row `from`, as windowedFocus takes it: ↑/↓ and the page
      keys a row, Home and End, → into an open row's first child, ← out to its parent. */
  function treeKey(key: string, from: number): number | null | undefined {
    const index = tree.indexes[from];
    switch (logicalKey(key)) {
      case "ArrowDown":
      case "PageDown":
        return from + 1;
      case "ArrowUp":
      case "PageUp":
        return from - 1;
      case "Home":
        return 0;
      case "End":
        return tree.indexes.length - 1;
      case "ArrowRight":
        return index != null && hasChildren(index) && !tree.collapsed.has(rowAt(index)!.key) ? from + 1 : null;
      case "ArrowLeft": {
        const parent = index == null ? -1 : parents[index]!;
        return parent >= 0 ? tree.indexes.indexOf(parent) : null;
      }
      default:
        return undefined;
    }
  }

  const rowFocus = windowedFocus(
    (position) => windowedRows?.scrollToIndex(position),
    () => tree.indexes.length,
    treeKey
  );

  /* Which rows the window has drawn. When the Tab stop isn't one of them, the row list itself
     is, and hands focus on to that row. */
  const drawn = new SvelteSet<string>();
  const trackDrawn =
    (key: string): Attachment =>
    () => {
      drawn.add(key);
      return () => drawn.delete(key);
    };

  function focusPosition(position: number, tries = 10): void {
    const row = rowsElement?.querySelector<HTMLElement>(`[data-index="${position}"]`);
    if (row) row.focus();
    else if (tries > 0) requestAnimationFrame(() => focusPosition(position, tries - 1));
  }

  /* Tabbed into from outside, not handed focus back by windowedFocus when a row scrolled away. */
  function enterRows(event: FocusEvent): void {
    const from = event.relatedTarget as Node | null;
    if (event.target !== rowsElement || !from || rowsElement?.contains(from)) return;
    const position = Math.max(0, tree.indexes.findIndex((index) => rowAt(index)?.key === tabKey));
    windowedRows?.scrollToIndex(position);
    focusPosition(position);
  }

  /** → opens a folded row and ← folds an open one; Enter seeks to it. windowedFocus moves focus
      for the other tree keys. Declared on the row through aria-keyshortcuts, which is what keeps
      the replay transport off them while it has focus. Capture, so a fold lands before the
      list would move focus. */
  function handleRowKeydown(event: KeyboardEvent, index: number): void {
    const row = rowAt(index);
    if (!row || event.target !== event.currentTarget) return;
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const key = logicalKey(event.key);
    const collapsed = tree.collapsed.has(row.key);
    if (key === "ArrowRight" && collapsed) toggleRow(row.key);
    else if (key === "ArrowLeft" && hasChildren(index) && !collapsed) toggleRow(row.key);
    else if (key === "Enter") onScrub(row.startIndex);
    else return;
    event.preventDefault();
    event.stopPropagation();
  }

  let measuringContext: CanvasRenderingContext2D | null = null;

  /** Null while the pane is too narrow for a label column. Read from the head cell, which is
      always drawn, in the rows' weight: the windowed rows mount later. */
  function measureLabels(headCell: HTMLElement, rows: readonly LabelRow[], carets: boolean): number | null {
    const title = headCell.firstElementChild;
    if (!rootElement || !title || getComputedStyle(title).display === "none") return null;
    measuringContext ??= document.createElement("canvas").getContext("2d");
    const context = measuringContext;
    if (!context) return null;
    const head = getComputedStyle(headCell);
    const root = getComputedStyle(rootElement);
    const token = (name: string) => Number.parseFloat(root.getPropertyValue(name)) || 0;
    context.font = `${root.fontWeight} ${head.fontSize} ${head.fontFamily}`;
    return labelContentWidth(rows, carets, {
      measure: (text) => context.measureText(text).width,
      padding: Number.parseFloat(head.paddingInlineStart) || 0,
      gap: token("--gap-xs"),
      caret: token("--caret"),
      indentStep: token("--indent-step"),
      indentCap: token("--indent-cap")
    });
  }

  /* The column fits the widest shown row. Within one run it only grows, so a streaming run's
     column holds still as durations tick, names arrive and the reader folds; another run, or a
     reload, measures afresh. Later changes to the rows or the pane's width settle for 200ms
     before they are measured. */
  let labelFit = $state({ run: Number.NaN, width: 0 });
  const labelRows = $derived(
    tree.indexes.map((index): LabelRow => {
      const row = rowAt(index)!;
      return { label: row.label, depth: row.depth, duration: durationLabel(row), count: tree.hidden[index] };
    })
  );
  const fitLabels: Attachment<HTMLElement> = (headCell) => {
    const run = startTs;
    const rows = labelRows;
    const carets = nested;
    void plotWidth;
    const fresh = untrack(() => labelFit.run) !== run;
    const fitNow = () => {
      const width = measureLabels(headCell, rows, carets);
      if (width == null) return;
      labelFit = { run, width: fresh ? width : Math.max(labelFit.width, width) };
    };
    if (fresh) return fitNow();
    const timer = window.setTimeout(fitNow, 200);
    return () => window.clearTimeout(timer);
  };
  const labelColumn = $derived(
    labelFit.width > 0 && plotWidth > 0 ? labelColumnWidth(labelFit.width, plotWidth) : null
  );

  function onPlotScroll(): void {
    if (!plotElement) return;
    scrollLeft = plotElement.scrollLeft;
    latestScrollLeft = scrollLeft;
    if (programmaticScroll) {
      programmaticScroll = false;
      return;
    }
    userScrolling = true;
    if (userScrollTimer) window.clearTimeout(userScrollTimer);
    userScrollTimer = window.setTimeout(() => {
      userScrolling = false;
    }, 150);
  }

  function startScrollbarGesture(event: PointerEvent): void {
    if (event.button === 0 && event.target === event.currentTarget) scrollbarGesture = true;
  }

  async function setZoom(nextZoom: number, anchorPx: number): Promise<void> {
    const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, nextZoom));
    if (clamped === zoom || !plotElement) return;
    const nextContentWidth = viewportWidth * clamped;
    const nextScale = buildWorkflowScale(
      { startTs, endTs },
      collapsedGaps,
      nextContentWidth
    );
    const nextScrollLeft = anchoredZoomScroll(
      scale,
      nextScale,
      scrollLeft,
      anchorPx,
      viewportWidth,
      nextContentWidth
    );
    suppressFollowOnce = true;
    zoom = clamped;
    scrollLeft = nextScrollLeft;
    latestScrollLeft = nextScrollLeft;
    await tick();
    if (!plotElement) return;
    programmaticScroll = true;
    plotElement.scrollLeft = nextScrollLeft;
  }

  function playheadAnchor(): number {
    return Math.min(viewportWidth, Math.max(0, (playheadX ?? scrollLeft + viewportWidth / 2) - scrollLeft));
  }

  function zoomBy(factor: number): void {
    void setZoom(zoom * factor, playheadAnchor());
  }

  async function fit(): Promise<void> {
    if (!plotElement) return;
    suppressFollowOnce = true;
    zoom = 1;
    scrollLeft = 0;
    latestScrollLeft = 0;
    await tick();
    if (!plotElement) return;
    programmaticScroll = true;
    plotElement.scrollLeft = 0;
  }

  function handleWheel(event: WheelEvent): void {
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    const rect = plotElement?.getBoundingClientRect();
    if (!rect) return;
    const anchor = Math.min(
      viewportWidth,
      Math.max(0, event.clientX - rect.left - labelWidth)
    );
    void setZoom(zoom * (event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP), anchor);
  }

  function handleKeydown(event: KeyboardEvent): void {
    const pane = rootElement?.closest(".pane");
    if (
      !pane?.matches(":focus-within") ||
      event.defaultPrevented ||
      event.isComposing ||
      event.altKey ||
      event.ctrlKey ||
      event.metaKey
    ) return;
    if (event.key === "+" || event.key === "=") zoomBy(ZOOM_STEP);
    else if (event.key === "-") zoomBy(1 / ZOOM_STEP);
    else if (event.key === "0") void fit();
    else return;
    event.preventDefault();
  }

</script>

{#snippet rowTip(row: WorkflowTimelineRow, index: number)}
  <TipCard
    heading={row.label}
    fields={[
      { key: "Kind", value: rowKindLabel(row) },
      { key: "Status", value: statusLabel(row) },
      { key: "Start", value: `+${formatDuration(row.startTs - startTs)}` },
      { key: "Duration", value: durationLabel(row) || "Instant" },
      { key: "Folded", value: tree.hidden[index] ? tree.summaries[index] : null }
    ]}
    body={row.detail}
  />
{/snippet}

<svelte:window
  onkeydown={handleKeydown}
  onpointerup={() => (scrollbarGesture = false)}
  onpointercancel={() => (scrollbarGesture = false)}
/>

<section
  class="workflow-timeline"
  aria-label="Timeline"
  aria-keyshortcuts="+ - 0"
  style:--label-width={labelColumn == null ? undefined : `${labelColumn}px`}
  {@attach rootNode}
>
  {#if timeline.workflow.rowCount === 0}
    <div class="empty" data-drawer-fit>
      <EmptyState title="Step through the stream to chart run-wide spans." />
    </div>
  {:else}
    <div
      class="plot-scroll"
      role="presentation"
      bind:clientWidth={plotWidth}
      {@attach plotNode}
      {@attach followPlayhead}
      onscroll={onPlotScroll}
      onwheel={handleWheel}
      onpointerdown={startScrollbarGesture}
    >
      <div
        class="plot-content"
        style:--content-width={`${contentWidth}px`}
        style:--shown-depth={shownDepth}
        style:--carets={nested ? 1 : 0}
      >
        <div class="axis-row">
          <div
            class="axis-label"
            data-tip="Zoom: ⌘/Ctrl + scroll, + or −&#10;Fit: 0"
            bind:clientWidth={labelWidth}
            {@attach fitLabels}
          >
            <span class="axis-title">Run</span>
          </div>
          <div
            class="axis"
            role="presentation"
            onpointerdown={startScrub}
            onpointermove={moveScrub}
            onpointerup={stopScrub}
            onpointercancel={stopScrub}
          >
            {#each runEdges as edge (edge.key)}
              <span class="run-rule" style:left={`${edge.px}px`} aria-hidden="true"></span>
            {/each}
            {#if playheadX != null}
              <span class="playhead" style:left={`${playheadX}px`} aria-hidden="true"></span>
            {/if}
            {#each visibleTicks as box (box.tick)}
              <span class="tick" style:left={`${box.left}px`}>{tickLabel(box.tick)}</span>
            {/each}
            <!-- An open gap is pressed across the whole stretch it opened, not at its middle:
                 a mark at the middle lands far from where the reader pressed, on a tick. -->
            {#each visibleGapControls as gap (gap.key)}
              {@const expanded = expandedGapKeys.includes(gap.key)}
              {@const tip = `${gapDuration(gap)} idle${expanded ? " · press to fold" : ""}`}
              <button
                type="button"
                class={["gap-toggle", expanded && "expanded"]}
                style:left={`${expanded ? projectTime(scale, gap.startTs) : gapCenter(gap)}px`}
                style:width={expanded ? `${projectTime(scale, gap.endTs) - projectTime(scale, gap.startTs)}px` : undefined}
                aria-label={`${expanded ? "Collapse" : "Expand"} ${gapDuration(gap)} idle gap`}
                aria-expanded={expanded}
                data-tip={tip}
                onpointerdown={(event) => event.stopPropagation()}
                onclick={() => toggleGap(gap)}
              >
                <svg viewBox="0 0 10 14" aria-hidden="true">
                  <path d={GAP_BREAK_PATH} vector-effect="non-scaling-stroke"></path>
                </svg>
              </button>
            {/each}
            {#each visibleCues as cue (cue.marker.id)}
              <button
                type="button"
                class={`cue ${cue.marker.tone}`}
                style:left={`${cue.x}px`}
                aria-label={`Jump to ${cue.marker.label}, turn ${cue.marker.turnNumber}`}
                onpointerdown={(event) => event.stopPropagation()}
                onclick={() => onScrub(cue.marker.index)}
              ></button>
            {/each}
          </div>
        </div>

        <div
          class="rows"
          data-drawer-fit
          role="treegrid"
          aria-label="Timeline rows"
          aria-rowcount={tree.indexes.length}
          tabindex={tabKey != null && !drawn.has(tabKey) ? 0 : -1}
          style:--plot-width={`${plotWidth}px`}
          onfocus={enterRows}
          {@attach rowsNode}
          {@attach rowFocus}
        >
          <WindowedRows
            bind:this={windowedRows}
            items={tree.indexes}
            getKey={(index) => rowAt(index)?.key ?? index}
            scroller={rowsElement}
            start={0}
            estimateSize={28}
          >
            {#snippet children(rowIndex, position)}
              {@const row = rowAt(rowIndex)}
              {#if row}
                {@const Icon = rowIcon(row)}
                {@const OutcomeIcon = row.tone === "error" ? XCircle : Icon}
                {@const expandable = hasChildren(rowIndex)}
                {@const hiddenChildren = tree.hidden[rowIndex]}
                {@const collapsed = tree.collapsed.has(row.key)}
                {@const offscreen = inlineLabels ? offscreenSide(row) : null}
                <!-- The kind's glyph is drawn in the caps, as Temporal draws one in every dot, so
                     the label is text and a parent's cell is the shared caret alone, turned down
                     while it is open. -->
                <div
                  class={["timeline-row", expandable && "disclosure", rowTone(row)]}
                  role="row"
                  aria-label={rowName(row, rowIndex)}
                  aria-level={row.depth + 1}
                  aria-rowindex={position + 1}
                  aria-expanded={expandable ? !collapsed : undefined}
                  aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Home End PageUp PageDown Enter"
                  tabindex={row.key === tabKey ? 0 : -1}
                  data-workflow-row={row.key}
                  data-index={position}
                  style:--depth={row.depth}
                  onfocus={(event) => {
                    focusedKey = row.key;
                    placeRowTip(event.currentTarget);
                  }}
                  onkeydowncapture={(event) => handleRowKeydown(event, rowIndex)}
                  {@attach trackDrawn(row.key)}
                >
                  <span class="row-label" role="gridcell" data-tip={inlineLabels ? undefined : ""}>
                    {#if expandable}
                      <button
                        type="button"
                        class="row-toggle"
                        tabindex="-1"
                        aria-label={`${collapsed ? "Expand" : "Collapse"} ${row.label}`}
                        onclick={() => toggleRow(row.key)}
                      >
                        <ChevronRight class="disclosure-chevron" size={12} aria-hidden="true" />
                      </button>
                    {:else if nested}
                      <span class="row-toggle" aria-hidden="true"></span>
                    {/if}
                    <span class={["row-name fade-end", hiddenChildren && "summarized"]} dir="auto">{row.label}</span>
                    {#if hiddenChildren}
                      {@const summary = tree.summaries[rowIndex] ?? ""}
                      <!-- One kind is its noun after the count, "+200 checks"; more are the breakdown. -->
                      <span class="row-count">+{hiddenChildren}</span>
                      <span class="row-summary fade-end">{summary.includes(" · ") ? summary : summary.slice(summary.indexOf(" ") + 1)}</span>
                    {/if}
                    <span class="row-duration">{durationLabel(row)}</span>
                    {#if !inlineLabels}{@render rowTip(row, rowIndex)}{/if}
                  </span>
                  <div
                    class="track"
                    role="gridcell"
                    tabindex="-1"
                    onpointerdown={startScrub}
                    onpointermove={moveScrub}
                    onpointerup={stopScrub}
                    onpointercancel={stopScrub}
                  >
                    <div
                      class="track-content"
                      style:left={`${-scrollLeft}px`}
                      style:width={`${contentWidth}px`}
                    >
                      {#each runEdges as edge (edge.key)}
                        <span class="run-rule" style:left={`${edge.px}px`} aria-hidden="true"></span>
                      {/each}
                      {#if playheadX != null}
                        <span class="playhead" style:left={`${playheadX}px`} aria-hidden="true"></span>
                      {/if}
                      {#each visibleGaps as gap (gap.key)}
                        <span
                          class="gap-break"
                          style:left={`${gapCenter(gap)}px`}
                          aria-hidden="true"
                        ></span>
                      {/each}
                      {#each ancestorsOf(rowIndex) as parent (parent.key)}
                        {@const from = projectTime(scale, parent.startTs) - CAP_HALF}
                        <span
                          class="parent-band"
                          style:left={`${from}px`}
                          style:width={`${projectTime(scale, spanEnd(parent)) + CAP_HALF - from}px`}
                          aria-hidden="true"
                        ></span>
                      {/each}
                      {#if isVisible(row)}
                        {@const box = geometry(row)}
                        {@const leadTip = inlineLabels && !offscreen ? "" : undefined}
                        <button
                          type="button"
                          class={markClass(row)}
                          style={markStyle(row, box)}
                          aria-label={markName(row)}
                          tabindex="-1"
                          onpointerdown={(event) => event.stopPropagation()}
                          onclick={() => onScrub(row.startIndex)}
                        >
                          <span class="mark-halo" aria-hidden="true"></span>
                          <span class={["bar", box.point && "point"]} aria-hidden="true">
                            {#if box.point}
                              <span class="cap point lead" data-tip={leadTip}>
                                <OutcomeIcon size={12} />
                                {#if leadTip != null}{@render rowTip(row, rowIndex)}{/if}
                              </span>
                            {:else}
                              <span class={["connector", row.ongoing && "dash"]}></span>
                              {#if box.showStart}
                                <span class="cap start lead" data-tip={leadTip}>
                                  <Icon size={12} />
                                  {#if leadTip != null}{@render rowTip(row, rowIndex)}{/if}
                                </span>
                              {/if}
                              {#if box.showEnd}
                                <span class="cap end"><OutcomeIcon size={12} /></span>
                              {/if}
                            {/if}
                          </span>
                        </button>
                        {#if inlineLabels && !offscreen}
                          {@const name = inlineLabel(row)}
                          {#if name}
                            <span
                              class={["inline-label fade-end", name.side]}
                              style:left={name.alignRight ? undefined : `${name.left}px`}
                              style:right={name.alignRight ? `${contentWidth - name.left - name.width}px` : undefined}
                              style:max-width={name.fade ? `${name.width}px` : undefined}
                              dir="auto"
                              aria-hidden="true"
                            >{row.label}</span>
                          {/if}
                        {/if}
                      {/if}
                    </div>
                    {#if offscreen}
                      <button
                        type="button"
                        class={["edge-label", offscreen]}
                        tabindex="-1"
                        data-tip=""
                        aria-label={`Jump to ${row.label}`}
                        onpointerdown={(event) => event.stopPropagation()}
                        onclick={() => onScrub(row.startIndex)}
                      >
                        {#if offscreen === "before"}<ChevronLeft size={12} aria-hidden="true" />{/if}
                        <span class="edge-name fade-end" dir="auto">{row.label}</span>
                        {#if offscreen === "after"}<ChevronRight size={12} aria-hidden="true" />{/if}
                        {@render rowTip(row, rowIndex)}
                      </button>
                    {/if}
                  </div>
                </div>
              {/if}
            {/snippet}
          </WindowedRows>
        </div>
      </div>
    </div>
  {/if}
</section>

<style>
  /* The label column is sized to its rows once they are measured (LABEL_WIDTH); until then it
     is the same clamp on the pane, not the window. The row's tooltip carries the full name. */
  .workflow-timeline {
    --label-width: clamp(120px, 30cqi, 220px);
    --caret: 14px;
    --indent-step: 12px;
    --indent-cap: 8;
    container-name: timeline;
    container-type: inline-size;
    width: 100%;
    height: 100%;
    min-height: 0;
    background: var(--surface-0);
  }

  .empty {
    padding: var(--pane-inset);
  }

  .plot-scroll {
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .plot-content {
    width: calc(var(--label-width) + var(--content-width));
    min-width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
  }

  .timeline-row {
    display: grid;
    grid-template-columns: var(--label-width) calc(var(--plot-width) - var(--label-width));
  }

  .axis-row {
    display: grid;
    grid-template-columns: var(--label-width) var(--content-width);
    min-height: 34px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-1);
  }

  .axis-label {
    position: sticky;
    z-index: 9;
    left: 0;
    align-self: end;
    height: 100%;
    display: flex;
    align-items: center;
    box-sizing: border-box;
    padding: 0 var(--pane-inset);
    background: var(--surface-1);
    color: var(--text-3);
    font-size: var(--font-xs);
    font-weight: 650;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  /* Clipped across only. The pane above the axis is often a drawer's top edge with the
     transport right over it, so a gap's tip has no room above and keepTipsInView
     (tipFocus.ts) flips it down over the rows — which it can only see, and only paint,
     while this lets it out vertically. */
  .axis {
    position: relative;
    width: var(--content-width);
    overflow: clip visible;
    direction: ltr;
    border-bottom: 1px solid var(--border-strong);
  }

  .axis,
  .track {
    cursor: grab;
    touch-action: none;
    user-select: none;
  }

  .axis:active,
  .track:active {
    cursor: grabbing;
  }

  .tick {
    position: absolute;
    bottom: 4px;
    color: var(--text-3);
    font-size: var(--font-xs);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .gap-toggle {
    position: absolute;
    z-index: 3;
    bottom: -1px;
    width: 14px;
    height: 20px;
    padding: 3px 2px;
    border: 0;
    background: var(--surface-1);
    color: var(--text-3);
    cursor: pointer;
    transform: translateX(-50%);
  }

  .gap-toggle svg {
    display: block;
    width: 10px;
    height: 14px;
    overflow: visible;
    fill: none;
    stroke: currentColor;
    stroke-width: 1;
  }

  /* The stretch an open gap gave back, hatched faintly the way a folded gap is in the rows,
     with its mark at the top, where an idle stretch has no cues and the ticks do not reach. */
  .gap-toggle.expanded {
    bottom: 0;
    height: 100%;
    display: grid;
    justify-items: center;
    align-items: start;
    padding: 2px 0 0;
    background: repeating-linear-gradient(
      135deg,
      transparent 0 6px,
      color-mix(in srgb, var(--border-strong) 40%, transparent) 6px 7px
    );
    color: var(--text-2);
    transform: none;
  }

  .gap-toggle.expanded:hover {
    background-color: var(--selected-bg);
  }

  .gap-toggle:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 1px;
  }

  .cue {
    position: absolute;
    z-index: 4;
    top: 0;
    width: 11px;
    height: 8px;
    margin-left: -5.5px;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: pointer;
  }

  .cue::before {
    content: "";
    position: absolute;
    top: 1px;
    left: 4px;
    width: 3px;
    height: 6px;
    background: var(--text-3);
  }

  .cue.approval::before { background: var(--live); }
  .cue.queue::before { background: var(--queue); }
  .cue.error::before { background: var(--error); }

  .cue:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -1px;
  }

  .rows {
    position: sticky;
    left: 0;
    width: var(--plot-width);
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
  }

  /* Indented a step per level, capped so a deep tree still leaves its names room; one
     guide per ancestor runs down under that ancestor's caret, in the chat activity feed's
     1px --border rule. On the row, not the label, because the label's pseudo-elements are
     its tooltip's. */
  .timeline-row {
    --indent: calc(min(var(--depth), var(--indent-cap)) * var(--indent-step));
    --guide-at: 7px;
    --guide-start: var(--pane-inset);
    --guides:
      transparent 0 calc(var(--guide-at) - 0.5px),
      var(--border) calc(var(--guide-at) - 0.5px) calc(var(--guide-at) + 0.5px),
      transparent calc(var(--guide-at) + 0.5px) var(--indent-step);
    position: relative;
    height: 28px;
    align-items: center;
    border-bottom: 1px solid var(--border);
    cursor: default;
  }

  .timeline-row:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: -2px;
  }

  /* The row takes keyboard focus, not the label, so the label's tip shows for it. */
  .timeline-row:focus-visible .row-label::before {
    display: block;
  }

  .timeline-row:focus-visible .row-label > :global(.tip-card) {
    display: grid;
  }

  .row-label {
    display: flex;
    align-items: center;
    gap: var(--gap-xs);
    align-self: stretch;
    min-width: 0;
    padding-block: 0;
    padding-inline: calc(var(--pane-inset) + var(--indent)) var(--gutter-tight);
    color: var(--text-2);
    font-size: var(--font-xs);
    white-space: nowrap;
  }

  .timeline-row::before {
    content: "";
    position: absolute;
    inset-block: 0;
    inset-inline-start: var(--guide-start);
    width: var(--indent);
    background: repeating-linear-gradient(to right, var(--guides));
    pointer-events: none;
  }

  .timeline-row:dir(rtl)::before {
    background: repeating-linear-gradient(to left, var(--guides));
  }

  .row-toggle {
    flex: 0 0 var(--caret);
    display: inline-grid;
    place-items: center;
    width: var(--caret);
    height: var(--caret);
    padding: 0;
    border: 0;
    border-radius: 3px;
    background: transparent;
    cursor: pointer;
  }

  /* With no glyph to stand in for, the caret is always drawn. */
  .row-toggle > :global(.disclosure-chevron) {
    opacity: 1;
  }

  .row-toggle:hover {
    background: var(--selected-bg);
  }

  .row-count {
    flex-shrink: 0;
    direction: ltr;
    unicode-bidi: isolate;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  .timeline-row.signal :is(.row-label, .inline-label:not(.inside)) {
    color: var(--text-1);
  }

  .timeline-row.routine :is(.row-label, .inline-label:not(.inside)) {
    color: var(--text-3);
  }

  .timeline-row.routine :is(.row-count, .row-summary, .row-duration) {
    color: var(--text-4);
  }

  /* Clipped by `.fade-end`; `match-parent` starts a name in the other direction at the row's
     start, not across the gap before its duration. */
  .row-name {
    flex: 1;
    min-width: 0;
    text-align: match-parent;
  }

  /* A folded row's name keeps its width and its summary takes only the room left over, so the
     summary fades out first and never widens the column. */
  .row-name.summarized {
    flex: 0 1 auto;
  }

  .row-summary {
    flex: 1 1 0;
    min-width: 0;
    color: var(--text-3);
  }

  .row-duration {
    flex-shrink: 0;
    color: var(--text-3);
    font-variant-numeric: tabular-nums;
  }

  /* Clipped across only, so a lead cap's tip can rise out of its row. Placed by column, so
     a narrow pane can drop the label cell before it. */
  .axis,
  .track {
    grid-column: 2;
  }

  .track {
    position: relative;
    height: 100%;
    min-width: 0;
    overflow: clip visible;
    direction: ltr;
  }

  .track-content {
    position: absolute;
    inset-block: 0;
    background-image: linear-gradient(
      to right,
      color-mix(in srgb, var(--border) 55%, transparent) 1px,
      transparent 1px
    );
    background-size: 25% 100%;
  }

  /* A parent's time range, cap to cap, behind each of its descendants' rows, as Temporal Web
     UI backs an open Event Group's rows: a fill and no stroke, since a hairline box is a
     control. Each level adds its own, so a grandchild sits in a deeper band. */
  .parent-band {
    position: absolute;
    inset-block: 0;
    background: color-mix(in srgb, var(--surface-3) 35%, transparent);
    pointer-events: none;
  }

  .playhead {
    position: absolute;
    z-index: 6;
    inset-block: 0;
    width: 1px;
    background: var(--text-1);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--surface-0) 60%, transparent);
    pointer-events: none;
  }

  /* The run's first and last pixel, in the axis and in each row, so no rule outlasts the rows. */
  .run-rule {
    position: absolute;
    z-index: 1;
    inset-block: 0;
    width: 1px;
    pointer-events: none;
    color: var(--text-3);
    background: color-mix(in srgb, currentColor 45%, transparent);
  }

  .gap-break {
    position: absolute;
    z-index: 2;
    inset-block: 0;
    width: 10px;
    opacity: 0.35;
    pointer-events: none;
    transform: translateX(-50%);
    background:
      linear-gradient(135deg, transparent 40%, var(--border-strong) 42% 52%, transparent 54%)
      0 0 / 8px 8px repeat-y;
  }

  /* Temporal's timeline proportions: the lane is the even size at or under three quarters of
     the 28px row, so it stays centred, and caps stand 1px proud of it on each side, as its
     20px dots do on an 18px line; its hit area is the whole row. */
  .mark {
    --cap: 22px;
    position: absolute;
    top: 4px;
    height: 20px;
    min-width: 4px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
    overflow: visible;
  }

  .mark::before {
    content: "";
    position: absolute;
    inset: -4px -6px;
  }

  .bar {
    position: absolute;
    inset: 0;
  }

  .connector {
    position: absolute;
    inset: 0;
    background: currentColor;
  }

  .state-ongoing .connector {
    overflow: hidden;
    background: transparent;
  }

  /* Temporal Web UI's pending line as a conveyor of the console's 1px hairlines: a stick every
     5px across the lane's height, scrolled one period per --dash-cycle. The scroll steps a
     whole pixel at a time, so a stick never sits between pixels and smears to 2px. */
  .state-ongoing .dash::before {
    content: "";
    position: absolute;
    inset: 0 -5px 0 0;
    background: repeating-linear-gradient(
      to right,
      currentColor 0 1px,
      transparent 1px 5px
    );
    animation: march var(--dash-cycle) steps(5) infinite;
    will-change: transform;
  }

  /* A wait on a person is time passing, not work: its lane is its state at 35%, as Temporal
     dims a scheduled activity's first stretch. Still open, the same dash runs over that tint. */
  .mark.wait .connector {
    background: color-mix(in srgb, currentColor 35%, transparent);
  }

  @keyframes march {
    to { transform: translateX(-5px); }
  }

  @media (prefers-reduced-motion: reduce) {
    .state-ongoing .dash::before {
      animation: none;
    }
  }

  /* As Temporal's dots: a state-coloured border on a darker fill of the same state, so the
     cap reads apart from the line, with the row's glyph in it. Its stroke is the console's
     1px hairline, drawn inset so the cap stays 22px. */
  .cap {
    --half: calc(var(--cap) / 2);
    position: absolute;
    top: 50%;
    width: var(--cap);
    height: var(--cap);
    display: grid;
    place-items: center;
    border-radius: var(--radius-xs);
    background: color-mix(in srgb, currentColor 24%, var(--surface-0));
    box-shadow: inset 0 0 0 1px currentColor;
    transform: translateY(-50%);
  }

  .cap :global(svg) {
    display: block;
  }

  .cap.start {
    left: calc(max(0px, var(--half) - var(--start-room)) - var(--half));
  }

  .cap.end {
    right: calc(max(0px, var(--half) - var(--end-room)) - var(--half));
  }

  /* An instant, or a span narrower than two caps, is one cap at its start, held inside the
     run's first and last pixel. */
  .cap.point {
    left: calc(min(var(--half), var(--end-room)) - min(var(--half), var(--start-room)) - var(--half));
  }

  .mark-halo {
    position: absolute;
    inset: -4px -14px;
    background: var(--selected-bg);
    opacity: 0;
    pointer-events: none;
  }

  .mark:hover .mark-halo {
    opacity: 1;
  }

  .kind-model { color: var(--model); }
  .kind-tool { color: var(--tool); }
  .kind-approval { color: var(--live); }
  .kind-evaluation { color: var(--reasoning); }
  .kind-subagent { color: var(--accent); }

  .mark.outcome-error .cap.end,
  .mark.outcome-error .cap.point {
    color: var(--error);
  }

  .mark:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  /* A narrow pane's name for the row, beside its span as Temporal Web UI prints one, in the
     label column's tone and size. On the lane it is the page's darkest surface on the kind's
     colour, which every solid kind holds at 4.5:1 or better. */
  .inline-label {
    position: absolute;
    top: 50%;
    display: none;
    color: var(--text-2);
    font-size: var(--font-xs);
    pointer-events: none;
    transform: translateY(-50%);
  }

  .inline-label.inside {
    color: var(--surface-0);
  }

  /* A wide pane names the row in its label column, so its lead cap's tip would repeat it. */
  .cap.lead::before,
  .cap.lead > :global(.tip-card) {
    display: none;
  }

  /* A row whose start is off the plot, named at the edge it went past. A fill and no stroke,
     so it reads over a lane as well as an empty track. */
  .edge-label {
    position: absolute;
    z-index: 7;
    top: 50%;
    display: none;
    align-items: center;
    gap: var(--gap-2xs);
    max-width: 100%;
    height: 20px;
    padding: 0 var(--gap-xs);
    border: 0;
    border-radius: var(--radius-xs);
    background: var(--surface-1);
    color: var(--text-3);
    font: inherit;
    font-size: var(--font-xs);
    cursor: pointer;
    transform: translateY(-50%);
  }

  /* The lane's height, flush with the plot's edge, so a lane it lies on does not show round it. */
  .edge-label.before {
    left: 0;
  }

  .edge-label.after {
    right: 0;
  }

  .edge-label:hover {
    background: var(--surface-2);
    color: var(--text-2);
  }

  /* Each is a stacking context (a z-index, a transform), so a tip would sit under the next row's
     label or cap unless the one showing it is lifted. */
  .edge-label:hover,
  .cap.lead:hover,
  .timeline-row:focus-visible :is(.edge-label, .cap.lead) {
    z-index: 40;
  }

  .edge-label > :global(svg) {
    flex-shrink: 0;
  }

  .edge-name {
    min-width: 0;
  }

  /* A narrow pane has no label column: the plot takes the width, the row's aria-label names
     it, and its lead cap's tip, or its edge label's while the cap is off the plot, shows on
     hover and keyboard focus. The gutter is the pane inset, 6px a level up to three, then the
     caret's cell when any row has children, so folding works through the same caret as in a
     wide pane. A 6px step is too short to draw a guide under the parent's caret, so the
     parent's band on the plot carries the nesting instead. */
  @container timeline (max-width: 559px) {
    .plot-content {
      --label-width: calc(var(--pane-inset) + min(var(--shown-depth), 3) * 6px + var(--carets) * var(--caret));
    }

    .axis-title,
    .row-label > :not(.row-toggle) {
      display: none;
    }

    .row-label {
      gap: 0;
      padding-inline-end: 0;
    }

    .timeline-row::before {
      display: none;
    }

    /* Kept, unpadded, as the gutter's measure: the plot's width is the pane's less it. */
    .axis-label {
      padding: 0;
    }

    .inline-label {
      display: block;
    }

    .timeline-row {
      --indent-step: 6px;
      --indent-cap: 3;
    }

    .edge-label {
      display: flex;
    }

    .cap.lead:hover::before,
    .timeline-row:focus-visible :is(.cap.lead, .edge-label)::before {
      display: block;
    }

    .cap.lead:hover > :global(.tip-card),
    .timeline-row:focus-visible :is(.cap.lead, .edge-label) > :global(.tip-card) {
      display: grid;
    }
  }
</style>
