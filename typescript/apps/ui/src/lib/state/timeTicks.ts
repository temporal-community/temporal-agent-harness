// ABOUTME: Evenly spaced, human-readable tick positions for a duration axis. Small enough to
// hand-roll; the whole point is picking a step a reader recognises (5s, 30s, 2m) instead of the
// arbitrary fraction that dividing a span by a tick count produces.

/* Steps a person reads without decoding: seconds, then the familiar minute multiples. */
const STEPS_SECONDS = [
  1, 2, 5, 10, 15, 20, 30,
  60, 120, 300, 600, 900, 1800,
  3600, 7200, 10800, 21600, 43200, 86400
];

/**
 * Tick values from 0 up to `span` seconds, inclusive of 0 and of the last tick at or below
 * `span`. Never returns fewer than two, so an axis always has a start and an end to read
 * between, and never divides by zero on a degenerate span.
 */
export function niceTimeTicks(span: number, targetCount = 6): number[] {
  if (!Number.isFinite(span) || span <= 0) return [0, 1];
  const target = Math.max(2, Math.floor(targetCount));

  const rough = span / target;
  const step = STEPS_SECONDS.find((candidate) => candidate >= rough) ?? STEPS_SECONDS.at(-1)!;

  const ticks: number[] = [];
  for (let value = 0; value <= span + 1e-9; value += step) {
    // Re-derive from the index rather than accumulating, so float drift cannot
    // shift a late tick off its label.
    ticks.push(Number((ticks.length * step).toFixed(6)));
    if (ticks.length > 512) break;
  }

  /* A span shorter than the smallest step leaves a single tick at zero, which is not an
     axis. Fall back to the span itself as the far end. */
  if (ticks.length < 2) return [0, span];
  return ticks;
}
