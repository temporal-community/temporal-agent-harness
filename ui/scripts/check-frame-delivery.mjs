import assert from "node:assert/strict";

import {
  catchUpCeilingMs,
  catchingUpAfterFrame,
  cursorAfterPublish,
  framePublishChunkSize,
  publishAtChunkBoundary
} from "../src/lib/state/hydration.ts";

/* The cursor rule. This is the scrub-during-live bug: frames kept arriving and
   each one moved the cursor to the newest event, so reading anything while a
   run was live was impossible. */

assert.equal(
  cursorAfterPublish(true, 40, 100),
  100,
  "following should tail the live edge"
);

assert.equal(
  cursorAfterPublish(false, 40, 100),
  40,
  "a parked cursor must survive a commit that added 60 events behind it"
);

assert.equal(
  cursorAfterPublish(false, 100, 100),
  100,
  "a cursor parked on what was the live edge should stay put, not drift"
);

/* The catch-up commit schedule. A short backlog arrives assembled; a long one
   starts showing progress rather than looking hung. */

assert.equal(
  publishAtChunkBoundary(false, 0),
  true,
  "outside a catch-up every chunk boundary should commit"
);

assert.equal(
  publishAtChunkBoundary(true, 0),
  false,
  "a catch-up that just started should stay quiet and arrive in one piece"
);

assert.equal(
  publishAtChunkBoundary(true, catchUpCeilingMs - 1),
  false,
  "still quiet just under the ceiling"
);

assert.equal(
  publishAtChunkBoundary(true, catchUpCeilingMs),
  true,
  "at the ceiling, silence starts to read as a hang, so show progress"
);

/* What the schedule costs, which is the regression being prevented. Committing
   per frame is what made hydration O(n^2): every commit re-runs the derived
   projections, so the count of commits is the thing to hold down. */

const hydrate = (frameCount, msPerChunk) => {
  let commits = 0;
  let elapsed = 0;
  let sinceCommit = 0;
  for (let index = 1; index <= frameCount; index += 1) {
    if (index % framePublishChunkSize !== 0) continue;
    elapsed += msPerChunk;
    sinceCommit += msPerChunk;
    if (publishAtChunkBoundary(true, sinceCommit)) {
      commits += 1;
      sinceCommit = 0; // the clock restart that makes the ceiling a rate limit
    }
  }
  return { commits: commits + 1, elapsed }; // +1 for the commit in the finally block
};

assert.equal(
  hydrate(1583, 0.5).commits,
  1,
  "a 1,583-frame session that hydrates quickly should commit once, not 1,583 times"
);

/* The ceiling has to bound commits by TIME. Bounding them per chunk instead
   looks fine until chunks pass faster than the page can paint. */
const slow = hydrate(1583, 40);
const allowed = Math.ceil(slow.elapsed / catchUpCeilingMs) + 1;
assert.ok(
  slow.commits <= allowed,
  `a ${slow.elapsed}ms hydration may commit at most ${allowed} times, got ${slow.commits}`
);
assert.ok(slow.commits > 1, "a multi-second hydration should still show progress");

assert.equal(
  hydrate(10, 0.5).commits,
  1,
  "a session shorter than one chunk should still commit once at the end"
);

const veryFastChunks = hydrate(100_000, 0.01);
assert.ok(
  veryFastChunks.commits <= Math.ceil(veryFastChunks.elapsed / catchUpCeilingMs) + 1,
  `100k frames arriving fast must not commit per chunk, got ${veryFastChunks.commits}`
);

/* History arriving over the STREAM rather than from cache. There is no loop to
   hang a schedule on here, only frames landing one at a time, so the schedule
   lives in #schedulePublish. Modelled below, because the failure it guards is
   silent and total: an early return while catching up publishes NOTHING for the
   whole backlog, and the console sits empty until the run goes live. */

const streamCatchUp = (replayFrames, liveFrames, msPerFrame, endStream = false) =>
  streamFrames(
    [...Array(replayFrames).fill(true), ...Array(liveFrames).fill(false)],
    msPerFrame,
    endStream
  );

/* Modelled on #appendFrame + #schedulePublish, driven by an explicit sequence of
   `replay` marks so an interleaved one can be fed in. `endStream` models the
   server closing the stream, which attach()'s finally block treats as a commit
   trigger in its own right. */
const streamFrames = (replayMarks, msPerFrame, endStream = false) => {
  let catchingUp = false;
  let liveFrameSeen = false;
  let catchUpStartedAt = 0;
  let sinceCatchUpPublish = 0;
  let clock = 0;
  const commits = [];

  const schedulePublish = () => {
    if (catchingUp) {
      sinceCatchUpPublish += 1;
      if (sinceCatchUpPublish < framePublishChunkSize) return;
      if (!publishAtChunkBoundary(true, clock - catchUpStartedAt)) return;
      sinceCatchUpPublish = 0;
      catchUpStartedAt = clock;
      commits.push({ at: clock, kind: "chunk" });
      return;
    }
    commits.push({ at: clock, kind: "paint" }); // rAF coalesces; one per frame here is the ceiling
  };

  const append = (isReplay) => {
    clock += msPerFrame;
    if (!isReplay) liveFrameSeen = true;
    const next = catchingUpAfterFrame(isReplay, liveFrameSeen);
    if (next !== catchingUp) {
      catchingUp = next;
      catchUpStartedAt = clock;
      sinceCatchUpPublish = 0;
      if (!next) commits.push({ at: clock, kind: "crossed-to-live" });
    }
    schedulePublish();
  };

  for (const isReplay of replayMarks) append(isReplay);
  if (endStream) commits.push({ at: clock, kind: "stream-end" });
  return commits;
};

// A fast cold load: 800 backlogged events then a quiet live edge. The backlog must
// not commit per frame, and the tail must not be stranded unpublished.
const coldFast = streamCatchUp(800, 1, 0.5);
assert.ok(
  coldFast.filter((c) => c.kind === "chunk").length === 0,
  "a sub-second backlog should arrive assembled, with no partial commits"
);
assert.ok(
  coldFast.some((c) => c.kind === "crossed-to-live"),
  "crossing to live must commit the tail of the backlog rather than hold it"
);

// A slow cold load. Progress has to show, but bounded by time, not by chunk count.
const coldSlow = streamCatchUp(2000, 1, 5);
const chunkCommits = coldSlow.filter((c) => c.kind === "chunk").length;
const elapsed = 2000 * 5;
assert.ok(
  chunkCommits > 0,
  "a ten-second backlog must show progress instead of looking hung"
);
assert.ok(
  chunkCommits <= Math.ceil(elapsed / catchUpCeilingMs) + 1,
  `a ${elapsed}ms stream catch-up may commit at most ~${Math.ceil(elapsed / catchUpCeilingMs)} times, got ${chunkCommits}`
);

// The regression this exists to prevent: publishing nothing at all during catch-up.
assert.ok(
  streamCatchUp(5000, 0, 5).length > 0,
  "a catch-up that never reaches the live edge must still publish something"
);

/* An idle session, which is the blank-console bug. The server replays the whole
   history and then ends the stream, so no frame ever crosses to live, and over a
   local socket the backlog is far too short and too fast to clear a chunk
   boundary past the ceiling. Nothing but the stream ending can commit these. */
assert.equal(
  streamCatchUp(27, 0, 9).length,
  0,
  "a 27-frame replay arriving in 250ms clears no chunk boundary on its own"
);
assert.equal(
  streamCatchUp(27, 0, 9, true).at(-1).kind,
  "stream-end",
  "the end of the stream must commit a backlog too short to reach a boundary"
);
assert.equal(
  streamCatchUp(6, 0, 9, true).length,
  1,
  "a session shorter than one chunk could never publish without a terminal commit"
);

/* What that terminal commit costs. A long slow backlog already commits on the
   chunk schedule, so the flush adds exactly one — per stream end, not per frame. */
assert.equal(
  streamCatchUp(2000, 1, 5, true).length - streamCatchUp(2000, 1, 5).length,
  1,
  "a terminal commit costs one publish per stream end, not one per frame"
);

// With no replay marker at all (an older server) every frame takes the paint path,
// which is exactly today's behavior — so the change cannot regress that case.
assert.equal(
  streamCatchUp(0, 30, 1).every((c) => c.kind === "paint"),
  true,
  "unmarked frames must keep the per-paint schedule"
);

/* Frames do not all arrive on one attach. Within a single attach the server's
   `replay` mark is ordered (it is resume_offset <= head, and resume_offset only
   advances), but a subagent gets a CONCURRENT attach with its own head, and its
   backlog is stamped replay while the root's frames are live. Merged into one
   pipeline the two orderings interleave, so the mode latches at the first live
   frame rather than reading the mark per frame. Proven in
   tests/harness/test_replay_stamp.py: ordered per attach, not across attaches. */

assert.equal(catchingUpAfterFrame(true, false), true, "a replay frame before the live edge is catch-up");
assert.equal(
  catchingUpAfterFrame(true, true),
  false,
  "a replay frame AFTER a live one is another stream's backlog, not a new catch-up"
);
assert.equal(catchingUpAfterFrame(false, true), false, "a live frame is never catch-up");

// The user's scenario: watching a session live while a subagent's own attach
// replays its history into the same pipeline.
const interleaved = [
  ...Array(200).fill(true),
  ...Array.from({ length: 600 }, (_, i) => i % 3 === 0)
];
const mixed = streamFrames(interleaved, 1);

/* Crossing to live publishes SYNCHRONOUSLY, so unlike a paint commit it is not
   coalesced by rAF: a burst of thirty events that should cost one commit costs
   one per flip instead. Bounding the crossings at one is what keeps a burst a
   burst. Pre-latch this sequence crossed 200 times. */
assert.equal(
  mixed.filter((c) => c.kind === "crossed-to-live").length,
  1,
  "a stream must cross to live exactly once, however the replay marks interleave"
);

/* The frames past the live edge must reach the per-paint schedule rather than
   being held for a chunk that a quiet run may never fill. */
assert.equal(
  mixed.filter((c) => c.kind === "chunk").length,
  0,
  "a replay mark arriving after the live edge must not re-enter chunked batching"
);

/* ponytail: bounding the commit COUNT is all this schedule does; each commit is
   still O(frames), measured at ~5us per frame across the derived projections, so
   one commit at 20k events costs ~100ms. Total catch-up work is
   (commits x frames), which the 1s ceiling keeps near-linear for a cold load but
   which stays quadratic for a long live session at one commit per paint.
   Upgrading that means making the projections incremental — appending to them
   rather than rebuilding them — not making the buffer copy cheaper, which is
   0.01% of a commit. */

console.log("frame delivery OK");
