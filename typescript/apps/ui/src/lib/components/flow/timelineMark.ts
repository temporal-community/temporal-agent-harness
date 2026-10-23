import type { TimelineSpan } from "$lib/state/stepTimeline";

export interface MarkGeometry {
  leftPct: number;
  widthPct: number;
  startPx: number;
  endPx: number;
  point: boolean;
}

export function projectMark(
  span: Pick<TimelineSpan, "startTs" | "durationSeconds">,
  turnStart: number,
  scale: number,
  trackPx: number
): MarkGeometry {
  const leftPct = Math.min(99.5, Math.max(0, ((span.startTs - turnStart) / scale) * 100));
  const widthPct = Math.max(
    0,
    Math.min(100 - leftPct, (span.durationSeconds / scale) * 100)
  );
  const startPx = (leftPct / 100) * trackPx;
  const endPx = ((leftPct + widthPct) / 100) * trackPx;
  return { leftPct, widthPct, startPx, endPx, point: widthPct === 0 };
}
