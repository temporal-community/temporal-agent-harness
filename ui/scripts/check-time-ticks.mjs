// ABOUTME: Asserts niceTimeTicks, the tick generator behind the usage chart's duration axis. The
// properties worth pinning are the ones that made it worth having over a chart library's own
// nicing: a seconds domain must step on values a reader recognises (30s, 2m, 1h) rather than the
// power of ten that grew a 20,000s axis under a 12,005s run, the spacing must be exactly even,
// every tick must sit inside the span, and a degenerate span must not divide by zero or return a
// single point.
//   node ui/scripts/check-time-ticks.mjs

import assert from "node:assert/strict";

// The module has no imports of its own, so Node's own type stripping is enough to load it
// directly rather than standing up a bundler for one function.
const { niceTimeTicks } = await import("../src/lib/state/timeTicks.ts");

function assertEvenlySpaced(ticks, label) {
  assert.ok(ticks.length >= 2, `${label}: needs at least two ticks, got ${ticks.length}`);
  const step = ticks[1] - ticks[0];
  assert.ok(step > 0, `${label}: step must be positive, got ${step}`);
  for (let i = 1; i < ticks.length; i++) {
    const gap = ticks[i] - ticks[i - 1];
    assert.ok(
      Math.abs(gap - step) < 1e-6,
      `${label}: spacing must be uniform; gap ${i} was ${gap}, expected ${step}`
    );
  }
}

// --- ordinary spans ------------------------------------------------------------------------------

for (const span of [7, 45, 83, 200, 610, 1800, 5400, 86_000]) {
  const ticks = niceTimeTicks(span, 6);
  assertEvenlySpaced(ticks, `span ${span}`);
  assert.equal(ticks[0], 0, `span ${span}: first tick is the origin`);
  assert.ok(
    ticks.at(-1) <= span + 1e-6,
    `span ${span}: last tick ${ticks.at(-1)} must not exceed the span`
  );
  // A ruler nobody can read defeats the purpose; keep the count in a sane band.
  assert.ok(
    ticks.length >= 2 && ticks.length <= 24,
    `span ${span}: produced ${ticks.length} ticks`
  );
}

// The step has to come off the recognisable list, not span/count. 83s over 6 wants ~13.8s,
// which must round to 15s rather than staying 13.8s.
assert.equal(niceTimeTicks(83, 6)[1], 15, "step snaps up to a readable 15s");
assert.equal(niceTimeTicks(610, 6)[1], 120, "a ten-minute span steps in whole minutes");
assert.equal(niceTimeTicks(45, 6)[1], 10, "a 45s span steps in 10s");

// --- degenerate spans ----------------------------------------------------------------------------

// This is the divide-by-zero guard the waterfall has always had, now also exercised here.
for (const span of [0, -1, -1000]) {
  const ticks = niceTimeTicks(span, 6);
  assert.deepEqual(ticks, [0, 1], `span ${span} floors to a unit axis`);
  assertEvenlySpaced(ticks, `span ${span}`);
}

for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
  assert.deepEqual(
    niceTimeTicks(bad, 6),
    [0, 1],
    `non-finite span ${bad} floors to a unit axis`
  );
}

// A span shorter than the smallest step still has to yield two readable ends.
const tiny = niceTimeTicks(0.4, 6);
assert.equal(tiny.length, 2, "a sub-second span yields exactly two ticks");
assert.equal(tiny[0], 0, "…starting at the origin");
assert.ok(tiny[1] > 0, "…and ending above it");

// --- target count is honoured but never fatal ----------------------------------------------------

for (const target of [0, 1, 2, 6, 20]) {
  const ticks = niceTimeTicks(600, target);
  assertEvenlySpaced(ticks, `target ${target}`);
}
assert.ok(
  niceTimeTicks(600, 3).length < niceTimeTicks(600, 12).length,
  "a larger target count produces more ticks"
);

// --- no float drift on a long axis ---------------------------------------------------------------

const long = niceTimeTicks(86_000, 6);
for (const tick of long) {
  assert.equal(
    tick,
    Math.round(tick),
    `long axis ticks stay on whole seconds, got ${tick}`
  );
}

console.log("time ticks ok");
