/**
 * When the frame pipeline commits what it has buffered.
 *
 * Streamed frames are staged in a plain array and published to reactive state in
 * batches, because publishing is the expensive half: every commit re-runs the
 * derived projections that draw the graph, the log and the transcript. One
 * commit per frame is what makes a long session hydrate in ten seconds.
 *
 * These are the policy decisions, kept pure so they can be checked without a
 * browser: npx vitest run src/lib/state/hydration.test.mjs.
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
 * Whether an arriving frame means "still catching up", given a live frame has or
 * hasn't already been seen since the stream opened.
 *
 * A catch-up is the head of a stream and it ends for good at the first live
 * frame, so the mode latches rather than tracking the server's `replay` mark
 * frame by frame.
 *
 * Within ONE attach the mark is already ordered — the server's `replay` is
 * `resume_offset <= head`, and `resume_offset` is a single counter that only
 * advances, so the mark goes True..True,False..False and never back. But frames
 * do not all come from one attach: a subagent gets its own concurrent attach
 * with its own head (see #attachWorkflow), and its backlog is stamped `replay`
 * while the root's frames are live. Merged into one pipeline, the two orderings
 * interleave, and reading the mark per frame flips the mode on every
 * alternation. Each flip back to live publishes synchronously, so an interleaved
 * burst costs one uncoalesced commit per frame — the exact cost the batching
 * exists to remove.
 *
 * Latching also gets the intent right: past the live edge, holding a frame back
 * for a chunk that may take a second to fill is the wrong trade for a view
 * someone is watching.
 */
export function catchingUpAfterFrame(isReplay: boolean, liveFrameSeen: boolean): boolean {
  return isReplay && !liveFrameSeen;
}

/**
 * The backlog a stream opens on arrives in a burst — a seven-second session replays
 * inside 250ms — so nothing counts as news until the view has listened past it.
 */
export const backlogGraceMs = 1_500;

/** How recently a thing must have happened, by its own timestamp, to count as news. */
export const liveSettleWindowSec = 10;

/**
 * Whether a tool that just settled is one the reader could have watched settle.
 *
 * Asked so that the focus canvas can hold a finished card for a beat instead of
 * blinking it out. The hold is only ever right for a settle that happened just
 * now: a page load pours the whole session back through the same pipeline, and
 * holding those would put one card per tool the session ever ran back on the
 * canvas at once — the exact clutter focus view exists to remove.
 *
 * `catchingUp` alone cannot answer it. That flag keys off the server's `replay`
 * mark, and the payloads /api/attach sends carry no such field, so on the path
 * that matters most every replayed frame reads as live. The frame's own age is
 * what settles it — a replayed one is minutes or days old where a live one is
 * milliseconds — and the grace window covers the case where the clock cannot be
 * trusted to say so. Both are kept because they fail in different directions.
 *
 * `frameAgeSec` is signed, and compared absolutely: a server clock a little ahead
 * of the browser's hands back a negative age for a frame that genuinely just
 * arrived. Past the window in either direction, nothing is held, which is this
 * feature switched off rather than misfiring.
 */
export function settleIsLive(
  catchingUp: boolean,
  msListening: number,
  frameAgeSec: number | null
): boolean {
  if (catchingUp || msListening < backlogGraceMs) return false;
  return frameAgeSec != null && Math.abs(frameAgeSec) < liveSettleWindowSec;
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
