/**
 * When the frame pipeline commits what it has buffered.
 *
 * Streamed frames are staged in a plain array and published to reactive state in
 * batches, because publishing is the expensive half: every commit re-runs the
 * derived projections that draw the graph, the log and the transcript. One
 * commit per frame is what makes a long session hydrate in ten seconds.
 *
 * These are the policy decisions, kept pure so they can be checked without a
 * browser: node ui/scripts/check-frame-delivery.mjs.
 */

/** Frames to ingest between catch-up commits, and between yields to the browser. */
export const framePublishChunkSize = 24;

/**
 * How long a catch-up may run before it starts showing its work.
 *
 * Below this, a catch-up commits only once at the end: a reload that replays
 * 1,500 cached frames should arrive assembled, not animate itself into being.
 * Past it the wait is long enough that silence reads as a hang, so partial
 * progress becomes the friendlier answer.
 */
export const catchUpCeilingMs = 1_000;

/** Whether a catch-up that has run this long should commit at a chunk boundary. */
export function publishAtChunkBoundary(
  catchingUp: boolean,
  msSinceCatchUpStarted: number
): boolean {
  return !catchingUp || msSinceCatchUpStarted >= catchUpCeilingMs;
}

/**
 * Where the cursor lands after a commit.
 *
 * Following means tail the live edge. Not following means someone scrubbed back
 * to read something, and arriving frames must not drag them forward off it —
 * which is exactly what an unconditional jump to the newest event does.
 */
export function cursorAfterPublish(
  following: boolean,
  viewIndex: number,
  total: number
): number {
  return following ? total : viewIndex;
}
