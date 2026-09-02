import assert from "node:assert/strict";

import {
  catchUpCeilingMs,
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

const streamCatchUp = (replayFrames, liveFrames, msPerFrame) => {
  let catchingUp = false;
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
    if (isReplay !== catchingUp) {
      catchingUp = isReplay;
      catchUpStartedAt = clock;
      sinceCatchUpPublish = 0;
      if (!isReplay) commits.push({ at: clock, kind: "crossed-to-live" });
    }
    schedulePublish();
  };

  for (let i = 0; i < replayFrames; i += 1) append(true);
  for (let i = 0; i < liveFrames; i += 1) append(false);
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

// With no replay marker at all (an older server) every frame takes the paint path,
// which is exactly today's behavior — so the change cannot regress that case.
assert.equal(
  streamCatchUp(0, 30, 1).every((c) => c.kind === "paint"),
  true,
  "unmarked frames must keep the per-paint schedule"
);

console.log("frame delivery OK");
