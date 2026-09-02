// ABOUTME: Asserts the two derivations the replay scrub lane is drawn from. The held scale is what
// makes a cue aimable during a live run: while a pointer is on the lane the denominator is pinned,
// so arriving events must move neither the playhead nor a mark, and the N/M reading must agree with
// the playhead rather than running past the end of the lane it is drawn on. The turn segments are
// the bar's chapters: they must partition the lane exactly (the last one reaching the end), fill
// nought-to-full across their own span rather than the whole run's, and produce no NaN on a run with
// no events — a NaN here reaches the DOM as `left: NaN%` and silently drops the whole bar.
//   node ui/scripts/check-scrub-lane.mjs

import assert from "node:assert/strict";

/* ponytail: ceiling = everything above the assertions is MIRRORED from
   ui/src/lib/components/flow/StepController.svelte rather than imported, so it
   can drift from the real thing. These are `$derived` expressions inside a
   Svelte component, so svelteLoader.mjs is not enough on its own: it can compile
   StepController.svelte, but a `$derived` closing over component state is not
   something the compiled module exports. Upgrade path = lift the derivations into
   plain functions beside eventVelocity.ts (which this repo's node can import
   directly) and import them here, which is what check-status-note.mjs did to
   statusKind once there was a loader to reach it through. */

// Kept in step with StepController.svelte.
function scaleOf(total, heldScale = null) {
  return Math.max(heldScale ?? total, 0);
}

function cursorPct(viewIndex, scale) {
  return scale === 0 ? 0 : Math.min((viewIndex / scale) * 100, 100);
}

function shownIndex(viewIndex, scale) {
  return Math.min(viewIndex, scale);
}

function cuesOf(anomalyMarkers, scale) {
  return anomalyMarkers.filter((marker) => marker.index <= scale);
}

function turnSegmentsOf(turnMarkers, viewIndex, scale) {
  if (scale === 0) return [];
  return turnMarkers.map((marker, position) => {
    const startIndex = marker.index;
    const endIndex =
      position + 1 < turnMarkers.length ? turnMarkers[position + 1].index : scale;
    const length = Math.max(endIndex - startIndex, 0);
    const filled = length === 0 ? 0 : (viewIndex - startIndex) / length;
    return {
      turnNumber: marker.turnNumber,
      leftPct: (startIndex / scale) * 100,
      widthPct: (length / scale) * 100,
      fillPct: Math.min(Math.max(filled, 0), 1) * 100
    };
  });
}

const finite = (value) => Number.isFinite(value);

// --- an empty run has no track to divide by ---------------------------------
// The lane used to floor the scale at 1, which printed "0/1" next to an aria
// reading of "0 of 0": two readings of one empty run, disagreeing on its length.
{
  const scale = scaleOf(0);
  assert.equal(scale, 0, "an empty run has a zero-length lane");
  assert.equal(cursorPct(0, scale), 0, "no events means no NaN playhead");
  assert.equal(shownIndex(0, scale), 0, "the reading agrees with the aria text");
  assert.deepEqual(turnSegmentsOf([], 0, scale), [], "no events, no chapters");
  assert.deepEqual(
    turnSegmentsOf([{ index: 0, turnNumber: 1 }], 0, scale),
    [],
    "a marker on an empty lane must not divide by zero"
  );
}

// --- the held scale keeps the lane still ------------------------------------
{
  const turnMarkers = [
    { index: 1, turnNumber: 1 },
    { index: 40, turnNumber: 2 },
    { index: 70, turnNumber: 3 }
  ];
  const anomalyMarkers = [
    { index: 30, turnNumber: 1, tone: "approval", label: "Approval requested" },
    { index: 95, turnNumber: 3, tone: "error", label: "Tool failed" },
    { index: 140, turnNumber: 4, tone: "error", label: "Agent error" }
  ];

  const held = scaleOf(100, 100);
  const grown = scaleOf(180, 100);
  assert.equal(grown, held, "a held scale ignores events that arrive after it");

  assert.equal(
    cursorPct(50, grown),
    cursorPct(50, held),
    "an arriving event must not slide the playhead out from under the pointer"
  );
  assert.deepEqual(
    turnSegmentsOf(turnMarkers, 50, grown),
    turnSegmentsOf(turnMarkers, 50, held),
    "an arriving event must not slide the chapters"
  );

  // Marks past the end of a held lane have nowhere to sit, so they wait.
  assert.deepEqual(
    cuesOf(anomalyMarkers, held).map((cue) => cue.index),
    [30, 95],
    "a cue past the held scale would ride out over the readout"
  );
  assert.equal(
    cuesOf(anomalyMarkers, scaleOf(180)).length,
    3,
    "releasing the scale brings the late cue back"
  );

  // The run can pass the held scale. The reading stops where the playhead does.
  assert.equal(cursorPct(160, held), 100, "the playhead stops at the end of the lane");
  assert.equal(shownIndex(160, held), 100, "and the reading stops with it");
  assert.equal(shownIndex(160, scaleOf(180)), 160, "off the lane it is just viewIndex");
}

// --- the chapters partition the lane ----------------------------------------
{
  const turnMarkers = [
    { index: 1, turnNumber: 1 },
    { index: 40, turnNumber: 2 },
    { index: 70, turnNumber: 3 }
  ];
  const segments = turnSegmentsOf(turnMarkers, 55, 100);

  assert.equal(segments.length, 3, "one chapter per turn");
  assert.ok(
    segments.every((s) => finite(s.leftPct) && finite(s.widthPct) && finite(s.fillPct)),
    "a NaN here reaches the DOM as `left: NaN%` and drops the bar"
  );

  // Contiguous, and the last one reaches the end of the lane.
  segments.forEach((segment, position) => {
    if (position === 0) return;
    const previous = segments[position - 1];
    assert.ok(
      Math.abs(previous.leftPct + previous.widthPct - segment.leftPct) < 1e-9,
      `chapter ${segment.turnNumber} should start where ${previous.turnNumber} ends`
    );
  });
  const last = segments[segments.length - 1];
  assert.ok(
    Math.abs(last.leftPct + last.widthPct - 100) < 1e-9,
    "the final chapter should reach the end of the lane"
  );

  // Fill is measured across a chapter's own span, not the whole run's.
  assert.equal(segments[0].fillPct, 100, "a turn the cursor is past is full");
  assert.equal(segments[2].fillPct, 0, "a turn the cursor has not reached is empty");
  const third = (55 - 40) / (70 - 40);
  assert.ok(
    Math.abs(segments[1].fillPct - third * 100) < 1e-9,
    `the current turn fills proportionally, expected ${third * 100}`
  );
}

// --- a turn with no events of its own ---------------------------------------
// Two markers on the same index: the empty one must not divide by zero.
{
  const segments = turnSegmentsOf(
    [
      { index: 10, turnNumber: 1 },
      { index: 10, turnNumber: 2 },
      { index: 50, turnNumber: 3 }
    ],
    30,
    100
  );
  assert.equal(segments[0].widthPct, 0, "an eventless turn takes no width");
  assert.equal(segments[0].fillPct, 0, "and reports no fill rather than NaN");
  assert.ok(segments.every((s) => finite(s.fillPct)), "still no NaN anywhere");
}

console.log("check-scrub-lane: held scale holds, chapters partition the lane, no NaN");
