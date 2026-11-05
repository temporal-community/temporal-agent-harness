// ABOUTME: Asserts every scroller's thumb is hidden at rest and shown on hover or while it
// scrolls, fading two idle seconds after it stops, as the pane rail's overlay bar does. The logs
// pane's filter row kept a horizontal thumb on screen at all times, over the filters.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, beforeEach, describe, it, vi } from "vitest";

import { SCROLL_IDLE_MS, watchScrollActivity } from "./scrollActivity.ts";

function element() {
  const attributes = new Set();
  return {
    attributes,
    setAttribute: (name) => attributes.add(name),
    removeAttribute: (name) => attributes.delete(name)
  };
}

describe("a scroller's thumb", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("is marked while it scrolls and unmarked two idle seconds after the last scroll", () => {
    const doc = new EventTarget();
    doc.documentElement = element();
    const stop = watchScrollActivity(doc);
    const pane = element();
    const scroll = (target) => {
      const event = new Event("scroll");
      Object.defineProperty(event, "target", { value: target });
      doc.dispatchEvent(event);
    };

    scroll(pane);
    assert.ok(pane.attributes.has("data-scrolling"));
    vi.advanceTimersByTime(SCROLL_IDLE_MS - 500);
    scroll(pane);
    vi.advanceTimersByTime(SCROLL_IDLE_MS - 500);
    assert.ok(pane.attributes.has("data-scrolling"), "a later scroll restarts the count");
    vi.advanceTimersByTime(500);
    assert.ok(!pane.attributes.has("data-scrolling"));

    scroll(doc);
    assert.ok(doc.documentElement.attributes.has("data-scrolling"), "the page itself marks the root");
    stop();
  });

  it("is hidden in the shared rule except on hover or while scrolling", () => {
    const css = readFileSync(new URL("../../../app.css", import.meta.url), "utf8");
    assert.match(css, /:not\(:hover, \[data-scrolling\]\)::-webkit-scrollbar-thumb \{\s*background: transparent;/);
    assert.match(css, /:where\(:not\(:hover, \[data-scrolling\]\)\) \{\s*scrollbar-color: transparent var\(--scrollbar-track\);/);
    assert.match(css, /:where\(:hover, \[data-scrolling\]\) \{\s*scrollbar-color: var\(--scrollbar-thumb\) var\(--scrollbar-track\);/);
    const main = readFileSync(new URL("../../../main.ts", import.meta.url), "utf8");
    assert.match(main, /watchScrollActivity\(document\);/, "the console watches every scroller");
  });
});
