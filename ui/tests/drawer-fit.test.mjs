// ABOUTME: Asserts that the bottom drawer is still fitted to the trace it holds, and still stops.
// Both halves have shipped broken. A drawer that is never refitted opens at 102px around a trace
// that had not arrived yet, because a link naming the drawer restores the pane before the session
// behind it streams anything; a drawer that refits forever climbs a row every turn until it has
// eaten the rail. What holds the line between them is which element is observed — the last row
// grows while its turn is live and is not resized by the turn after it — so that is what this
// checks, along with the observer being given up when the drawer is.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const source = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");

/* This reads App.svelte and proves the fit is WIRED, not that it lands on the
   right pixel — there is no layout engine here, and a ResizeObserver has nothing
   to report to one. The pixel was pinned by a browser probe that needed a dev
   server and so could never live in `just app-check`; that probe is gone. This
   belongs in a browser check if a headless browser ever becomes a dependency of
   this UI. */

/* The whole of the wiring, read as one region: three unrelated mentions scattered
   through a thousand-line component would satisfy the claims below without any of
   them being about the drawer. */
const OPENS = "new ResizeObserver(";
const CLOSES = "observer.disconnect()";

describe("the bottom drawer is fitted to the trace it holds", () => {
  it("re-measures from the row the fit is read off", () => {
    const opens = source.indexOf(OPENS);
    const closes = source.indexOf(CLOSES);
    assert.ok(opens > -1, "the drawer's fit must be driven by a ResizeObserver");
    assert.ok(closes > opens, "the observer must be disconnected after it is created");

    const wiring = source.slice(opens, closes);
    assert.match(
      wiring,
      /fitDrawerToContent\(\)/,
      "the observer must refit the drawer, or it is watching for nothing"
    );
    assert.match(
      wiring,
      /querySelector\("\.turns"\)/,
      "the fit is measured off the trace's scroller, so that is what it must look in"
    );
    assert.match(
      wiring,
      /lastElementChild/,
      "the last row is the live one: watching anything else either never fires or never stops"
    );
    assert.match(wiring, /\.observe\(/, "the row has to actually be observed");
  });

  /* An observer outliving the drawer holds the detached rows of every session the
     tab has opened, and goes on measuring them. */
  it("gives the observer up when the drawer goes", () => {
    assert.match(
      source,
      /return \(\) => observer\.disconnect\(\);/,
      "the effect must disconnect the observer on teardown"
    );
  });

  /* The poll this replaced sampled every 400ms and gave up after twenty tries, so a
     run that changed shape for eight seconds straight stopped being fitted. Nothing
     may count measurements again: the observer stops when the content does. */
  it("does not give up after a fixed number of measurements", () => {
    const wiring = source.slice(source.indexOf(OPENS), source.indexOf(CLOSES));
    assert.doesNotMatch(
      wiring,
      /setTimeout|setInterval/,
      "the fit is event-driven now; a timer here is a poll wearing an observer's name"
    );
    assert.doesNotMatch(
      wiring,
      /\btries\b/i,
      "a bounded number of measurements is the ceiling this replaced"
    );
  });
});
