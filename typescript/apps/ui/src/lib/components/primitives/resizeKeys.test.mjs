// ABOUTME: Pins keyboard resizing on both docked drawers and the pane gutters that share its rule.
// A bottom drawer grows with ArrowUp and a left one with ArrowRight — the arrow along the axis the
// seam slides on — and Home asks for what double-click does. A step keeps the drag's limits: below
// the minimum the drawer shuts, it never passes its maximum, and growing a shut drawer opens it at
// its minimum, or the keyboard could never reopen it. Modified arrows are the rail's, not the
// gutter's. The gutter itself is a focusable separator announcing its size.

import assert from "node:assert/strict";
import { createRawSnippet } from "svelte";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import DockedDrawer from "./DockedDrawer.svelte";
import {
  RESIZE_STEP,
  drawerResizeKeys,
  resizeKeyIntent,
  restoredDrawerSize,
  stepDrawerSize
} from "./resizeKeys.ts";

const key = (name, modifiers = {}) => ({
  key: name,
  altKey: false,
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  ...modifiers
});
const MIN = 120;
const MAX = 600;

/** The size one key leaves a drawer at, or "reset". */
function afterKey(edge, size, name) {
  const intent = resizeKeyIntent(key(name), drawerResizeKeys(edge));
  if (intent == null || intent === "reset") return intent;
  return stepDrawerSize(size, intent, MIN, MAX);
}

describe("keyboard resizing a docked drawer", () => {
  it("moves each edge along its own axis, and ignores the other", () => {
    assert.equal(afterKey("bottom", 300, "ArrowUp"), 300 + RESIZE_STEP);
    assert.equal(afterKey("bottom", 300, "ArrowDown"), 300 - RESIZE_STEP);
    assert.equal(afterKey("bottom", 300, "ArrowLeft"), null);
    assert.equal(afterKey("left", 300, "ArrowRight"), 300 + RESIZE_STEP);
    assert.equal(afterKey("left", 300, "ArrowLeft"), 300 - RESIZE_STEP);
    assert.equal(afterKey("left", 300, "ArrowUp"), null);
  });

  it("asks for what double-click does on Home", () => {
    assert.equal(afterKey("bottom", 300, "Home"), "reset");
    assert.equal(afterKey("left", 300, "Home"), "reset");
  });

  it("stays within the drag's limits", () => {
    for (const [edge, grow, shrink] of [
      ["bottom", "ArrowUp", "ArrowDown"],
      ["left", "ArrowRight", "ArrowLeft"]
    ]) {
      assert.equal(afterKey(edge, MAX - 5, grow), MAX, `${edge} stops at its maximum`);
      assert.equal(afterKey(edge, MAX, grow), MAX);
      assert.equal(afterKey(edge, MIN + 5, shrink), 0, `${edge} shuts below its minimum`);
      assert.equal(afterKey(edge, 0, shrink), 0);
      assert.equal(afterKey(edge, 0, grow), MIN, `${edge} reopens at its minimum`);
    }
  });

  it("restores a saved size inside this window, and nothing that was not a size", () => {
    assert.equal(restoredDrawerSize(300, MIN, MAX), 300);
    assert.equal(restoredDrawerSize(900, MIN, MAX), MAX, "a taller window's size is brought inside");
    assert.equal(restoredDrawerSize(300, MIN, MIN - 20), MIN, "never restored below its minimum");
    assert.equal(restoredDrawerSize(0, MIN, MAX), null, "a drawer saved shut is not a size");
    assert.equal(restoredDrawerSize(undefined, MIN, MAX), null);
    assert.equal(restoredDrawerSize("300", MIN, MAX), null);
    assert.equal(restoredDrawerSize(Number.NaN, MIN, MAX), null);
  });

  it("leaves modified arrows to the rail", () => {
    for (const modifier of ["altKey", "metaKey", "ctrlKey", "shiftKey"]) {
      assert.equal(resizeKeyIntent(key("ArrowUp", { [modifier]: true }), "up-down"), null);
    }
  });

  it("renders the gutter as a focusable separator that states its size", () => {
    const children = createRawSnippet(() => ({ render: () => "<div>body</div>" }));
    const gutterOf = (edge) =>
      render(DockedDrawer, {
        props: {
          edge,
          label: "Drawer",
          size: 300,
          minSize: MIN,
          maxSize: MAX,
          onResize: () => {},
          onFit: () => {},
          children
        }
      }).body.match(/<div[^>]*class="drawer-gutter[" ][^>]*>/)[0];

    const bottom = gutterOf("bottom");
    assert.match(bottom, /role="separator"/);
    assert.match(bottom, /tabindex="0"/);
    assert.match(bottom, /aria-orientation="horizontal"/);
    assert.match(bottom, /aria-valuenow="300"/);
    assert.match(bottom, /aria-valuemin="0"/);
    assert.match(bottom, /aria-valuemax="600"/);
    assert.match(bottom, /aria-label="Resize the bottom drawer"/);
    assert.match(gutterOf("left"), /aria-orientation="vertical"/);
  });
});
