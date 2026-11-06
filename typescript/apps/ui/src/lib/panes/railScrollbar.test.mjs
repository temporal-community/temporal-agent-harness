// ABOUTME: Asserts the pane rail's overlay scrollbar: its thumb tracks the scroll position and a
// drag maps back to it, and it shows on activity, fades after two idle seconds, and stays while
// hovered or dragged. The rail's own bar took 8px of height whenever the desk overflowed, so every
// pane jumped up and down as it came and went.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRawSnippet } from "svelte";
import { render } from "svelte/server";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import { createPaneStack } from "../state/paneStack.svelte.ts";
import PaneRail from "./PaneRail.svelte";
import { AutoHide, railThumb, scrollForThumb } from "./railScrollbar.svelte.ts";

/* Anything inside a scroller counts toward its scroll width. A bar kept in view there by the
   scroll offset held the rail open at its widest after the columns that filled it were closed,
   so closing Logs from a scrolled desk left all of its room empty after the last column. */
describe("the rail's overlay bar sits beside the scroller", () => {
  it("is not inside the rail, and does not follow its scroll", () => {
    const html = render(PaneRail, {
      props: {
        stack: createPaneStack(),
        describe: (pane) => ({ title: pane.kind }),
        paneContent: createRawSnippet(() => ({ render: () => "<div></div>" }))
      }
    }).body;
    const rail = html.search(/<div class="rail[ "]/);
    const bar = html.indexOf('<div class="rail-scrollbar');
    assert.ok(html.indexOf('<div class="rail-frame') < rail, "the frame holds both");
    assert.ok(rail >= 0 && bar > rail, "the bar comes after the rail");
    const between = html.slice(rail, bar);
    const depth = (between.match(/<div\b/g) ?? []).length - (between.match(/<\/div>/g) ?? []).length;
    assert.equal(depth, 0, "the rail is closed before the bar opens");
    assert.doesNotMatch(html.slice(bar, html.indexOf(">", bar)), /style=/, "no width or offset from the scroll");

    const css = readFileSync(new URL("./PaneRail.svelte", import.meta.url), "utf8");
    assert.match(css, /\.rail-scrollbar \{\s*position: absolute;\s*left: 0;\s*right: 0;\s*bottom: 0;/);
  });
});

describe("the rail's overlay thumb", () => {
  it("is absent when nothing scrolls, and spans the visible share of the desk", () => {
    assert.equal(railThumb(0, 1000, 1000), null);
    assert.equal(railThumb(0, 0, 500), null);
    assert.deepEqual(railThumb(0, 1000, 2000), { left: 0, width: 500 });
    assert.deepEqual(railThumb(1000, 1000, 2000), { left: 500, width: 500 }, "at the end");
    assert.equal(railThumb(0, 1000, 100_000).width, 32, "never too thin to grab");
  });

  it("maps a dragged thumb back to the same scroll position", () => {
    const { left, width } = railThumb(400, 1000, 2000);
    assert.equal(scrollForThumb(left, 1000, 2000, width), 400);
    assert.equal(scrollForThumb(-50, 1000, 2000, width), 0, "clamped at the start");
    assert.equal(scrollForThumb(9999, 1000, 2000, width), 1000, "and at the end");
  });
});

describe("the rail's overlay bar auto-hide", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows on activity and fades after two idle seconds, counted from the last", () => {
    const bar = new AutoHide();
    assert.equal(bar.visible, false);
    bar.poke();
    assert.equal(bar.visible, true);
    vi.advanceTimersByTime(1500);
    bar.poke();
    vi.advanceTimersByTime(1500);
    assert.equal(bar.visible, true, "a second scroll restarts the count");
    vi.advanceTimersByTime(500);
    assert.equal(bar.visible, false);
  });

  it("stays while hovered or dragged, and fades two seconds after it is let go", () => {
    const bar = new AutoHide();
    bar.hold(true);
    vi.advanceTimersByTime(10_000);
    assert.equal(bar.visible, true);
    bar.hold(false);
    vi.advanceTimersByTime(1999);
    assert.equal(bar.visible, true);
    vi.advanceTimersByTime(1);
    assert.equal(bar.visible, false);
    bar.dispose();
  });
});
