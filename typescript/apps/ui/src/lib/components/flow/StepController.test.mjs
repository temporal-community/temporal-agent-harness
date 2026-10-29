// ABOUTME: Asserts the two derivations the replay scrub lane is drawn from. The held scale is what
// makes a cue aimable during a live run: while a pointer is on the lane the denominator is pinned,
// so arriving events must move neither the playhead nor a mark, and the N/M reading must agree with
// the playhead rather than running past the end of the lane it is drawn on. The turn segments are
// the bar's chapters: they must partition the lane exactly (the first starting at its left edge,
// the last reaching the end), fill
// nought-to-full across their own span rather than the whole run's, and produce no NaN on a run with
// no events — a NaN here reaches the DOM as `left: NaN%` and silently drops the whole bar.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import { describe, it } from "vitest";
import StepController from "./StepController.svelte";

/* These were hand-copied here for as long as they were `$derived` expressions
   inside StepController.svelte: vite hands back the component's module, but a
   `$derived` closing over component state is not something that module exports,
   so there was nothing to import. They are plain functions in scrubLane.ts now
   and the copies are gone — a test that reimplements what it tests passes
   forever while the real thing rots. */
import {
  laneCues,
  lanePct,
  laneReading,
  laneScale,
  laneSegments
} from "$lib/state/scrubLane.ts";

const finite = (value) => Number.isFinite(value);

describe("the replay scrub lane", () => {
  // --- an empty run has no track to divide by ---------------------------------
  // The lane used to floor the scale at 1, which printed "0/1" next to an aria
  // reading of "0 of 0": two readings of one empty run, disagreeing on its length.
  it("has no track to divide by on an empty run", () => {
    const scale = laneScale(0);
    assert.equal(scale, 0, "an empty run has a zero-length lane");
    assert.equal(lanePct(0, scale), 0, "no events means no NaN playhead");
    assert.equal(laneReading(0, scale), 0, "the reading agrees with the aria text");
    assert.deepEqual(laneSegments([], 0, scale), [], "no events, no chapters");
    assert.deepEqual(
      laneSegments([{ index: 0, turnNumber: 1 }], 0, scale),
      [],
      "a marker on an empty lane must not divide by zero"
    );
  });

  // --- the held scale keeps the lane still ------------------------------------
  it("keeps the lane still while the scale is held", () => {
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

    const held = laneScale(100, 100);
    const grown = laneScale(180, 100);
    assert.equal(grown, held, "a held scale ignores events that arrive after it");

    assert.equal(
      lanePct(50, grown),
      lanePct(50, held),
      "an arriving event must not slide the playhead out from under the pointer"
    );
    assert.deepEqual(
      laneSegments(turnMarkers, 50, grown),
      laneSegments(turnMarkers, 50, held),
      "an arriving event must not slide the chapters"
    );

    // Marks past the end of a held lane have nowhere to sit, so they wait.
    assert.deepEqual(
      laneCues(anomalyMarkers, held).map((cue) => cue.index),
      [30, 95],
      "a cue past the held scale would ride out over the readout"
    );
    assert.equal(
      laneCues(anomalyMarkers, laneScale(180)).length,
      3,
      "releasing the scale brings the late cue back"
    );

    // The run can pass the held scale. The reading stops where the playhead does.
    assert.equal(lanePct(160, held), 100, "the playhead stops at the end of the lane");
    assert.equal(laneReading(160, held), 100, "and the reading stops with it");
    assert.equal(laneReading(160, laneScale(180)), 160, "off the lane it is just viewIndex");
  });

  // --- the chapters partition the lane ----------------------------------------
  it("partitions the lane into chapters", () => {
    const turnMarkers = [
      { index: 1, turnNumber: 1 },
      { index: 40, turnNumber: 2 },
      { index: 70, turnNumber: 3 }
    ];
    const segments = laneSegments(turnMarkers, 55, 100);

    // The event before turn 1 is its own chapter: starting at turn 1 left the bar
    // bare there, a dark notch at the start of the lane.
    assert.deepEqual(
      segments.map((s) => s.turnNumber),
      [0, 1, 2, 3],
      "one chapter per turn, and one for what came before the first"
    );
    assert.deepEqual(segments.map((s) => s.index), [0, 1, 40, 70]);
    assert.equal(segments[0].leftPct, 0, "the first chapter starts at the lane's left edge");
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
    assert.equal(segments[1].fillPct, 100, "a turn the cursor is past is full");
    assert.equal(segments[3].fillPct, 0, "a turn the cursor has not reached is empty");
    const third = (55 - 40) / (70 - 40);
    assert.ok(
      Math.abs(segments[2].fillPct - third * 100) < 1e-9,
      `the current turn fills proportionally, expected ${third * 100}`
    );

    assert.deepEqual(
      laneSegments([], 3, 8).map((s) => [s.leftPct, s.widthPct]),
      [[0, 100]],
      "a run with events but no turn still has a track to fill"
    );
  });

  // --- a turn with no events of its own ---------------------------------------
  // Two markers on the same index: the empty one must not divide by zero.
  it("gives a turn with no events of its own no width and no NaN", () => {
    const segments = laneSegments(
      [
        { index: 10, turnNumber: 1 },
        { index: 10, turnNumber: 2 },
        { index: 50, turnNumber: 3 }
      ],
      30,
      100
    );
    assert.equal(segments[1].widthPct, 0, "an eventless turn takes no width");
    assert.equal(segments[1].fillPct, 0, "and reports no fill rather than NaN");
    assert.ok(segments.every((s) => finite(s.fillPct)), "still no NaN anywhere");
  });
});

const controllerProps = {
  viewIndex: 12,
  total: 24,
  playing: false,
  following: false,
  playbackSpeed: 1,
  currentEvent: null,
  turnMarkers: [
    { index: 1, turnNumber: 1 },
    { index: 12, turnNumber: 2 }
  ],
  anomalyMarkers: [
    { index: 8, turnNumber: 1, tone: "approval", label: "Approval requested" }
  ],
  eventRows: Array.from({ length: 25 }, (_, index) => ({ index, timestamp: index })),
  drawerOpen: false,
  onToggleDrawer() {},
  onPlay() {},
  onPause() {},
  onSpeedChange() {},
  onJumpToLive() {},
  onReset() {},
  onScrub() {}
};

describe("the transport lane", () => {
  it("draws the full scrub lane by default", () => {
    const html = render(StepController, { props: controllerProps }).body;
    assert.match(html, /class="ridge\b/);
    assert.match(html, /class="tip\b/);
    assert.match(html, /<span class="segment\b/);
    assert.doesNotMatch(html, /class="scrub[^"]*\boverview\b/);
  });

  it("draws a thin interactive overview without computing the ridge", () => {
    const eventRows = [{ index: 0, get timestamp() { throw new Error("ridge computed"); } }];
    const html = render(StepController, {
      props: { ...controllerProps, overview: true, eventRows }
    }).body;
    assert.match(html, /class="scrub[^"]*\boverview\b/);
    assert.match(html, /<span class="segment\b/);
    assert.match(html, /<input class="scrub-input\b[^>]*type="range"/);
    assert.doesNotMatch(html, /<button[^>]*class="segment\b/);
    assert.match(html, /aria-valuetext="Replay start, event 12 of 24"/);
    assert.doesNotMatch(html, /class="(?:ridge|tip|knob)\b/);

    const source = readFileSync(new URL("./StepController.svelte", import.meta.url), "utf8");
    const segmentLoop = source.match(/{#each turnSegments[\s\S]*?{\/each}/)?.[0] ?? "";
    assert.doesNotMatch(segmentLoop, /{#if overview}/);
    assert.doesNotMatch(segmentLoop, /<button\b/);
  });
});

describe("the turn chapters after a continue-as-new", () => {
  // Turn numbers restart at 1 on a roll-over, and keying the chapters by turn number threw
  // each_key_duplicate on every render of the run. Chapters are positional; nothing else is
  // unique about two of them.
  it("are not keyed by turn number", () => {
    const markers = [
      { index: 4, turnNumber: 1 },
      { index: 10, turnNumber: 2 },
      { index: 20, turnNumber: 1 }
    ];
    const segments = laneSegments(markers, 25, 30);
    assert.deepEqual(
      segments.map((s) => s.turnNumber),
      [0, 1, 2, 1],
      "the prelude and a repeated turn number each get their own chapter"
    );

    const source = readFileSync(new URL("./StepController.svelte", import.meta.url), "utf8");
    assert.doesNotMatch(source, /#each turnSegments as [^}]*\([^)]*turnNumber[^)]*\)/);
  });
});

/* The event card was its own box with a head of "event N of M" the readout under it already
   showed, and at 420px it hung 162px off the window's left edge. */
describe("the replay event card", () => {
  const source = readFileSync(new URL("./StepController.svelte", import.meta.url), "utf8");
  const css = source.slice(source.indexOf("<style"));

  it("is the shared tip card, holding only what the bar does not already show", () => {
    assert.match(source, /<TipCard\s+class="now-card"\s+id="now-card"[\s\S]{0,200}?heading=\{eventTitle\}\s+fields=\{eventFields\}\s+body=\{eventBody\}/);
    const fields = source.slice(source.indexOf("const eventFields"), source.indexOf("const nextSpeed"));
    assert.doesNotMatch(fields, /of \$\{total\}|viewIndex/, "the readout has the position");
    assert.match(fields, /key: "Agent", value: row\.parentTurnNumber == null \? null : row\.sourceLabel/, "the header has the run's own agent");
    assert.match(fields, /row\.timestamp > 0 \?/, "a zero timestamp is left out, not shown as 1970");
    assert.doesNotMatch(css, /\.now-(head|type|where|body)\b/, "no second copy of the card's layout");
  });

  it("scrolls a long event instead of cutting it off, and fits a narrow window", () => {
    const card = css.match(/\n {2}\.now :global\(\.now-card\) \{([^}]*)\}/)?.[1] ?? "";
    assert.match(card, /width: min\(340px, calc\(100vw - 2 \* var\(--gutter\)\)\);/);
    assert.match(card, /overflow-y: auto;/);
    assert.doesNotMatch(card, /line-clamp|border:|box-shadow/, "a hairline box reads as pressable");
    assert.match(css, /@media \(max-width: 760px\) \{[^@]*\.now \{\s*position: static;\s*\}\s*\.now :global\(\.now-card\) \{\s*right: auto;\s*left: var\(--gutter\);/);
  });
});

describe("the position readout", () => {
  it("colors the fraction and does not draw a status swatch", () => {
    const html = render(StepController, {
      props: {
        ...controllerProps,
        currentEvent: { tone: "done", label: "Tool finished", timestamp: 0 }
      }
    }).body;
    const nowAt = html.search(/class="now\b/);
    const asideAt = html.search(/class="aside\b/);
    const readout = html.slice(nowAt, asideAt);
    assert.match(readout, />12\/24</);
    assert.match(readout, /class="[^"]*\bpos\b[^"]*\bdone\b/);
    assert.match(readout, /aria-label="12\/24, done"/);
    assert.doesNotMatch(readout, /class="[^"]*\bdot\b/);
    assert.match(html.slice(0, nowAt), /class="[^"]*\bdot\b/);

    const resting = render(StepController, { props: controllerProps }).body;
    const restingReadout = resting.slice(resting.search(/class="now\b/), resting.search(/class="aside\b/));
    const pos = restingReadout.match(/<span class="[^"]*\bpos\b[^"]*"/)?.[0] ?? "";
    assert.match(pos, /\bpos\b/);
    assert.doesNotMatch(pos, /\b(done|error|approval|queue)\b/);
    assert.doesNotMatch(restingReadout, /class="[^"]*\bdot\b/);
    assert.doesNotMatch(restingReadout, /aria-label=/);

    const css = readFileSync(new URL("./StepController.svelte", import.meta.url), "utf8");
    assert.match(css, /\.pos\.done\s*\{[^}]*color:\s*var\(--success\)/);
    assert.match(css, /\.pos\.error\s*\{[^}]*color:\s*var\(--error\)/);
    assert.match(css, /\.pos\.approval\s*\{[^}]*color:\s*var\(--live\)/);
    assert.match(css, /\.pos\.queue\s*\{[^}]*color:\s*var\(--queue\)/);
  });
});
