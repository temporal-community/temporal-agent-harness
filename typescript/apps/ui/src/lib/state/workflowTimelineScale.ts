import { niceTimeTicks } from "$lib/state/timeTicks";

export interface WorkflowTimelineBounds {
  startTs: number;
  endTs: number;
}

export interface CollapsedGap {
  key: string;
  startTs: number;
  endTs: number;
}

export interface ScaledSegment {
  key: string;
  startTs: number;
  endTs: number;
  startPx: number;
  endPx: number;
  collapsed: boolean;
}

export interface TimelineSpanRange {
  startTs: number;
  endTs: number;
}

const expandedKey = (startTs: number, endTs: number) => `expanded:${startTs}:${endTs}`;

/** The "≀" drawn on a collapsed gap, as points in a 10×14 box, top to bottom. */
export const GAP_BREAK_GLYPH: readonly (readonly [number, number])[] = [[1, 0], [7, 4], [3, 9], [9, 14]];
export const GAP_BREAK_PATH = `M${GAP_BREAK_GLYPH.map(([x, y]) => `${x} ${y}`).join("L")}`;

/** Where a collapsed gap's break sits, and where anything inside the gap is pinned. */
export const gapBreakX = (segment: ScaledSegment) => (segment.startPx + segment.endPx) / 2;
/** Half the width of the timeline axis's break button, which is 14px wide. */
export const GAP_BREAK_HALF_WIDTH = 7;

export function buildWorkflowScale(
  bounds: WorkflowTimelineBounds,
  collapsedGaps: readonly CollapsedGap[],
  contentWidthPx: number,
  collapsedWidthPx = 48
): ScaledSegment[] {
  const startTs = Number.isFinite(bounds.startTs) ? bounds.startTs : 0;
  const endTs = Number.isFinite(bounds.endTs) ? Math.max(startTs, bounds.endTs) : startTs;
  const width = Number.isFinite(contentWidthPx) ? Math.max(0, contentWidthPx) : 0;
  const gapWidth = Number.isFinite(collapsedWidthPx) ? Math.max(0, collapsedWidthPx) : 0;

  const gaps = collapsedGaps
    .map((gap) => ({
      ...gap,
      startTs: Math.max(startTs, gap.startTs),
      endTs: Math.min(endTs, gap.endTs)
    }))
    .filter(
      (gap) =>
        Number.isFinite(gap.startTs) &&
        Number.isFinite(gap.endTs) &&
        gap.endTs > gap.startTs
    )
    .sort((a, b) => a.startTs - b.startTs || a.endTs - b.endTs);

  const mergedGaps: CollapsedGap[] = [];
  for (const gap of gaps) {
    const previous = mergedGaps.at(-1);
    if (previous && gap.startTs <= previous.endTs) {
      previous.endTs = Math.max(previous.endTs, gap.endTs);
    } else {
      mergedGaps.push({ ...gap });
    }
  }

  const collapsedDuration = mergedGaps.reduce(
    (total, gap) => total + gap.endTs - gap.startTs,
    0
  );
  const expandedDuration = endTs - startTs - collapsedDuration;
  const expandedWidth = Math.max(0, width - mergedGaps.length * gapWidth);
  const pixelsPerTime = expandedDuration > 0 ? expandedWidth / expandedDuration : 0;

  const segments: ScaledSegment[] = [];
  let cursorTs = startTs;
  let cursorPx = 0;
  for (const gap of mergedGaps) {
    if (gap.startTs > cursorTs) {
      const endPx = cursorPx + (gap.startTs - cursorTs) * pixelsPerTime;
      segments.push({
        key: expandedKey(cursorTs, gap.startTs),
        startTs: cursorTs,
        endTs: gap.startTs,
        startPx: cursorPx,
        endPx,
        collapsed: false
      });
      cursorPx = endPx;
    }
    segments.push({
      key: gap.key,
      startTs: gap.startTs,
      endTs: gap.endTs,
      startPx: cursorPx,
      endPx: cursorPx + gapWidth,
      collapsed: true
    });
    cursorTs = gap.endTs;
    cursorPx += gapWidth;
  }

  if (cursorTs < endTs || segments.length === 0) {
    segments.push({
      key: expandedKey(cursorTs, endTs),
      startTs: cursorTs,
      endTs,
      startPx: cursorPx,
      endPx: cursorPx + (endTs - cursorTs) * pixelsPerTime,
      collapsed: false
    });
  }
  return segments;
}

function segmentAt(
  segments: readonly ScaledSegment[],
  value: number,
  end: (segment: ScaledSegment) => number
): ScaledSegment | undefined {
  let low = 0;
  let high = segments.length - 1;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (value <= end(segments[middle])) high = middle;
    else low = middle + 1;
  }
  return segments[low];
}

function interpolate(value: number, start: number, end: number, outputStart: number, outputEnd: number) {
  if (end <= start) return outputStart;
  return outputStart + ((value - start) / (end - start)) * (outputEnd - outputStart);
}

export function projectTime(segments: readonly ScaledSegment[], timestamp: number): number {
  if (segments.length === 0) return 0;
  const first = segments[0];
  const last = segments.at(-1)!;
  const clamped = Math.min(last.endTs, Math.max(first.startTs, timestamp));
  const segment = segmentAt(segments, clamped, ({ endTs }) => endTs) ?? first;
  return interpolate(clamped, segment.startTs, segment.endTs, segment.startPx, segment.endPx);
}

export function unprojectTime(segments: readonly ScaledSegment[], px: number): number {
  if (segments.length === 0) return 0;
  const first = segments[0];
  const last = segments.at(-1)!;
  const clamped = Math.min(last.endPx, Math.max(first.startPx, px));
  const segment = segmentAt(segments, clamped, ({ endPx }) => endPx) ?? first;
  return interpolate(clamped, segment.startPx, segment.endPx, segment.startTs, segment.endTs);
}

export function visibleTimeRange(
  segments: readonly ScaledSegment[],
  scrollLeft: number,
  viewportWidth: number,
  overscanPx: number
): [number, number] {
  const left = Math.max(0, scrollLeft - Math.max(0, overscanPx));
  const right = Math.max(left, scrollLeft + Math.max(0, viewportWidth) + Math.max(0, overscanPx));
  return [unprojectTime(segments, left), unprojectTime(segments, right)];
}

export function visibleTimelineSpans<T extends TimelineSpanRange>(
  spans: readonly T[],
  startTs: number,
  endTs: number
): readonly T[] {
  let low = 0;
  let high = spans.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (spans[middle].endTs < startTs) low = middle + 1;
    else high = middle;
  }
  const first = low;
  while (low < spans.length && spans[low].startTs <= endTs) low += 1;
  return spans.slice(first, low);
}

export function anchoredZoomScroll(
  current: readonly ScaledSegment[],
  next: readonly ScaledSegment[],
  scrollLeft: number,
  anchorPx: number,
  viewportWidth: number,
  contentWidth: number
): number {
  const timestamp = unprojectTime(current, scrollLeft + anchorPx);
  const anchored = projectTime(next, timestamp) - anchorPx;
  return Math.min(Math.max(0, contentWidth - viewportWidth), Math.max(0, anchored));
}

export function followPlayheadScroll(
  playheadPx: number,
  scrollLeft: number,
  viewportWidth: number,
  contentWidth: number,
  suspended = false,
  gutterRatio = 0.15
): number {
  if (suspended || viewportWidth <= 0) return scrollLeft;
  const gutter = viewportWidth * gutterRatio;
  const left = scrollLeft + gutter;
  const right = scrollLeft + viewportWidth - gutter;
  const next = playheadPx < left
    ? playheadPx - gutter
    : playheadPx > right
      ? playheadPx - viewportWidth + gutter
      : scrollLeft;
  return Math.min(Math.max(0, contentWidth - viewportWidth), Math.max(0, next));
}

/* `spacingPx` leaves room for an "1h 00m 00s" label between ticks, and the final tick, drawn
   ending at the run's edge, takes that whole width from the tick before it. */
export function workflowScaleTicks(
  segments: readonly ScaledSegment[],
  spacingPx = 96
): number[] {
  if (segments.length === 0) return [];
  const spacing = Math.max(1, spacingPx);
  const ticks = new Set<number>();
  for (const segment of segments) {
    if (segment.collapsed) continue;
    const targetCount = Math.max(2, Math.ceil((segment.endPx - segment.startPx) / spacing));
    for (const offset of niceTimeTicks(segment.endTs - segment.startTs, targetCount)) {
      if (offset <= segment.endTs - segment.startTs) ticks.add(segment.startTs + offset);
    }
  }
  const endTs = segments.at(-1)!.endTs;
  const endPx = projectTime(segments, endTs);
  ticks.add(segments[0]!.startTs);
  return [
    ...[...ticks].filter((tick) => endPx - projectTime(segments, tick) >= spacing),
    endTs
  ].sort((a, b) => a - b);
}

/**
 * The ticks whose labels differ. The two either side of a collapsed gap can round to the same
 * label ("2s ≀ 2s"); the first of a run of equal labels is kept, except that the run's last tick
 * always stays and takes the place of the one before it.
 */
export function distinctTickLabels(
  ticks: readonly number[],
  label: (tick: number) => string
): number[] {
  const kept: number[] = [];
  for (const tick of ticks) {
    const previous = kept.at(-1);
    if (previous === undefined || label(previous) !== label(tick)) kept.push(tick);
  }
  const last = ticks.at(-1);
  if (last !== undefined && kept.at(-1) !== last && kept.length > 1) kept[kept.length - 1] = last;
  return kept;
}

export interface TickLabelBox {
  tick: number;
  left: number;
  right: number;
}

/**
 * Each tick label's box in px. A label hangs away from the run's edges and from a fold's break,
 * so neither cuts it: inward at the first and last tick, outward at either side of a fold,
 * centred elsewhere. When the stretch after the last fold is narrower than the end label, the
 * label moves before the break instead, and a tick label it would then cover is dropped: the
 * run's total time is the one worth keeping. The start label does the same at a leading fold.
 */
export function tickLabelBoxes(
  segments: readonly ScaledSegment[],
  ticks: readonly number[],
  width: (tick: number) => number,
  gapPx = 4
): TickLabelBox[] {
  const folds = segments.filter((segment) => segment.collapsed);
  const boxes = ticks.map((tick, index) => {
    const x = projectTime(segments, tick);
    const w = width(tick);
    const left =
      index === 0 || folds.some((fold) => fold.endTs === tick)
        ? x
        : index === ticks.length - 1 || folds.some((fold) => fold.startTs === tick)
          ? x - w
          : x - w / 2;
    return { tick, left, right: left + w };
  });
  if (boxes.length === 0) return boxes;
  const breaks = folds.map((fold) => ({
    left: gapBreakX(fold) - GAP_BREAK_HALF_WIDTH - gapPx,
    right: gapBreakX(fold) + GAP_BREAK_HALF_WIDTH + gapPx
  }));
  const overlaps = (box: TickLabelBox, other: { left: number; right: number }) =>
    box.left < other.right && box.right > other.left;
  const start = boxes[0]!;
  for (const area of breaks) {
    if (overlaps(start, area)) [start.left, start.right] = [area.right, area.right + start.right - start.left];
  }
  const end = boxes.at(-1)!;
  if (end === start) return boxes;
  for (const area of breaks.toReversed()) {
    if (overlaps(end, area)) [end.left, end.right] = [area.left - (end.right - end.left), area.left];
  }
  /* Between two folds a label hung out from one break can reach the label hung back from the
     next: the later of two that meet is dropped. */
  const kept = [start];
  for (const box of boxes.slice(1, -1)) {
    if (box.left >= kept.at(-1)!.right + gapPx && box.right + gapPx <= end.left) kept.push(box);
  }
  return [...kept, end];
}

export type InlineLabelSide = "after" | "before" | "inside";

export interface InlineLabelBox {
  side: InlineLabelSide;
  left: number;
  width: number;
  /** Narrower than the name, so it fades out at its end. */
  fade: boolean;
  /** Set against the cap after it, so it is placed by its right edge. */
  alignRight: boolean;
}

export interface InlineLabelSpan {
  /** The outer edges of the span's first and last cap, or of its one cap. */
  outer: readonly [number, number];
  /** The solid lane between its caps, when the name may be printed on it. */
  lane?: readonly [number, number];
  plotWidth: number;
  /** Where the row's gap breaks are drawn; a name stops short of them. */
  breaks: readonly number[];
  /** The name's width, and how many characters it has. */
  width: number;
  chars: number;
  gap: number;
  rtl: boolean;
}

/** Where a narrow pane prints a span's name on its own row, as Temporal Web UI does: after
    its last cap, else before its first, else on its lane, at full width; failing that, faded
    in the widest of those rooms, or nowhere when under three characters would show. Right to
    left, the room before the span is tried first. */
export function inlineLabelBox(span: InlineLabelSpan): InlineLabelBox | null {
  const { outer: [start, end], lane, plotWidth, breaks, width, chars, gap, rtl } = span;
  if (width <= 0 || chars <= 0) return null;
  const nextBreak = (x: number) =>
    Math.min(plotWidth, ...breaks.filter((at) => at > x).map((at) => at - GAP_BREAK_HALF_WIDTH));
  const previousBreak = (x: number) =>
    Math.max(0, ...breaks.filter((at) => at < x).map((at) => at + GAP_BREAK_HALF_WIDTH));
  const rooms: [InlineLabelSide, number, number][] = [
    ["after", end + gap, nextBreak(end)],
    ["before", previousBreak(start), start - gap]
  ];
  if (rtl) rooms.reverse();
  if (lane) rooms.push(["inside", lane[0] + gap, Math.min(lane[1] - gap, nextBreak(lane[0]))]);
  const size = ([, from, to]: (typeof rooms)[number]) => to - from;
  const place = ([side, from, to]: (typeof rooms)[number], w: number, fade: boolean): InlineLabelBox => {
    const alignRight = side === "before" || (side === "inside" && rtl);
    return { side, left: alignRight ? to - w : from, width: w, fade, alignRight };
  };
  const fits = rooms.find((room) => size(room) >= width);
  if (fits) return place(fits, width, false);
  const widest = rooms.reduce((best, room) => (size(room) > size(best) ? room : best));
  return size(widest) >= width * Math.min(1, 3 / chars) ? place(widest, size(widest), true) : null;
}
