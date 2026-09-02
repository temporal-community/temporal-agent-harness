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
