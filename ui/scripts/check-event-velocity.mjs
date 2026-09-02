// ABOUTME: Asserts the density ribbon drawn behind the replay scrubber. Three properties matter: a
// run too short to have a shape draws nothing rather than a misleading flat line, a burst of tool
// calls really does tower over a turn that sat waiting on a human (the ribbon is events per SECOND,
// not per bucket of index, which would be a constant), and events published past the held scale
// leave the shape alone — that last one is the whole promise of pinning the scale while a pointer is
// on the lane, and it is what makes a cue aimable during a live run. Also asserts the path stays
// inside its 0..1 plot box, because a peak flat against the edge loses half its stroke to the clip
// and reads as cropped data.
//   node ui/scripts/check-event-velocity.mjs

import assert from "node:assert/strict";

import { eventVelocity, velocityPath } from "../src/lib/state/eventVelocity.ts";

/* Kept in step with the module's own private constants. These are assertions
   about the shape of the output, so naming them here rather than exporting them
   keeps the module's surface to the two functions the lane actually calls. */
const MAX_BUCKETS = 48;
const TOP_MARGIN = 0.04;

// A run with no velocity story leaves the lane alone.
assert.equal(
  eventVelocity([{ index: 1, timestamp: 0 }], 1).length,
  0,
  "a one-event run should draw no ribbon"
);
assert.equal(velocityPath([]), "", "no values is no path");
assert.equal(velocityPath([0.5]), "", "a single value is not a shape");

// 200 events: the first half a burst at 20/s, the second half one every 5s.
const burst = [];
for (let index = 1; index <= 100; index += 1) {
  burst.push({ index, timestamp: index * 0.05 });
}
const slowStart = burst[99].timestamp;
for (let index = 101; index <= 200; index += 1) {
  burst.push({ index, timestamp: slowStart + (index - 100) * 5 });
}

const values = eventVelocity(burst, 200);
assert.equal(values.length, MAX_BUCKETS, `expected ${MAX_BUCKETS} buckets`);
assert.ok(
  values.every((value) => value >= 0 && value <= 1),
  "velocity should be normalised into 0..1"
);

const early = values[2];
const late = values[values.length - 3];
assert.ok(early > late * 2, `burst should tower over the wait, got ${early} vs ${late}`);

/* The held-scale promise: the lane is drawn against `scale`, so events the run
   has published past it must not change the shape under the pointer. */
const grown = [...burst];
for (let index = 201; index <= 260; index += 1) {
  grown.push({ index, timestamp: slowStart + 500 + index * 0.01 });
}
assert.deepEqual(
  eventVelocity(grown, 200),
  values,
  "events past the held scale moved the ribbon"
);

const path = velocityPath(values);
assert.ok(path.startsWith("M0.00,"), "path should start at the left edge");
assert.ok(path.includes("L100.00,"), "path should reach the right edge");

const ys = [...path.matchAll(/,(\d+\.\d+)/g)].map((match) => Number(match[1]));
assert.ok(
  ys.every((y) => y >= TOP_MARGIN - 1e-9 && y <= 1 - TOP_MARGIN + 1e-9),
  "path left the plot box, so a peak would be clipped"
);

console.log(`check-event-velocity: ${values.length} buckets, held scale holds, path in box`);
