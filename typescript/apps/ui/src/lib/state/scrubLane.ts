/**
 * Where things sit on the replay scrub lane.
 *
 * One denominator — the scale — and everything the lane draws placed against it:
 * the playhead, the N/M reading beside it, the anomaly cues, and the per-turn
 * chapters under the bar. They are pure arithmetic over an index and that scale,
 * so they live here rather than as `$derived` expressions inside StepController:
 * a `$derived` closing over component state is not something the module exports,
 * and a test that cannot reach the real thing ends up keeping a copy of it.
 *
 * Beside eventVelocity.ts on purpose. That is the lane's other geometry, drawn
 * against the same scale for the same reason.
 */

import type { ReplayMarker } from "./replayLog";

/** A turn's first event, which is where its chapter begins. */
export interface TurnMark {
  index: number;
  turnNumber: number;
}

/** One chapter of the bar: where it starts, how wide it is, how full. */
export interface TurnSegment {
  index: number;
  turnNumber: number;
  leftPct: number;
  widthPct: number;
  fillPct: number;
}

/**
 * The scale the lane is drawn against, held still while the pointer is on it.
 *
 * `heldScale` is the total as it stood when the pointer arrived, so arriving
 * events move nothing under a hand that is reaching for a mark. Not floored at
 * one: dividing an empty run by 1 is what printed "0/1" beside an aria reading
 * of "0 of 0", two readings of one empty run disagreeing about its length.
 */
export function laneScale(total: number, heldScale: number | null = null): number {
  return Math.max(heldScale ?? total, 0);
}

/** Where an index sits along the lane. Stops at the end, because the lane does. */
export function lanePct(index: number, scale: number): number {
  return scale === 0 ? 0 : Math.min((index / scale) * 100, 100);
}

/**
 * The N in the "N/M" reading, which agrees with the playhead rather than running
 * past the end of the lane the playhead is drawn on. Off a held scale this is
 * just the view index.
 */
export function laneReading(viewIndex: number, scale: number): number {
  return Math.min(viewIndex, scale);
}

/**
 * The cues with somewhere to sit. The lane does not clip, so a mark past a held
 * scale would ride out over the readout; it comes back when the scale does.
 */
export function laneCues(markers: ReplayMarker[], scale: number): ReplayMarker[] {
  return markers.filter((marker) => marker.index <= scale);
}

/**
 * The chapters of the track, one per turn, each filling across its own span so
 * the bar reads as a sequence rather than one undifferentiated line. They
 * partition the lane exactly: the first starts at its left edge and the last
 * reaches the end.
 *
 * Events a run publishes before its first turn — state an agent registers in
 * `@workflow.init` — get a chapter 0 of their own. Starting at turn 1 instead
 * left that stretch as bare bar, a dark notch at the start of every such run,
 * and a run with no turns at all had no track to fill.
 *
 * A run with no events has no track to place them on, and a turn with no events
 * of its own has no span to divide by — both would otherwise reach the DOM as
 * `left: NaN%`, which silently drops the whole bar.
 */
export function laneSegments(
  turnMarks: TurnMark[],
  viewIndex: number,
  scale: number
): TurnSegment[] {
  if (scale === 0) return [];
  const chapters =
    (turnMarks[0]?.index ?? scale) > 0 ? [{ index: 0, turnNumber: 0 }, ...turnMarks] : turnMarks;
  return chapters.map((mark, position) => {
    const startIndex = mark.index;
    const endIndex = position + 1 < chapters.length ? chapters[position + 1].index : scale;
    const length = Math.max(endIndex - startIndex, 0);
    const filled = length === 0 ? 0 : (viewIndex - startIndex) / length;
    return {
      index: startIndex,
      turnNumber: mark.turnNumber,
      leftPct: (startIndex / scale) * 100,
      widthPct: (length / scale) * 100,
      fillPct: Math.min(Math.max(filled, 0), 1) * 100
    };
  });
}
