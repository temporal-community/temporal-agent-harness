// ABOUTME: Asserts that every rail column owns its width: dragging one changes that one and no
// other, and neither a window resize nor a reload changes any. Seam drags used to trade width with
// the neighbour, and before that the stylesheet shared it out among every column, while a fit
// scaled saved widths to the window.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, describe, it } from "vitest";

import { PANE_META, SPINE_SIZE } from "../panes/registry.ts";
import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { createPaneStack, slotKey } from "./paneStack.svelte.ts";

const SESSION = "wf-own-widths";
const rail = readFileSync(new URL("../panes/PaneRail.svelte", import.meta.url), "utf8");
const widths = (stack) =>
  Object.fromEntries(stack.groups.map((group) => [slotKey(group), stack.sizeOfGroup(group)]));

describe("a column's width", () => {
  afterEach(() => {
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.sessionStorage;
  });

  it("is drawn as is, and dragging one leaves every other alone across a resize and a reload", () => {
    assert.match(rail, /\n {2}\.rail-slot \{[^}]*flex: 0 0 var\(--slot-size\);/, "no column grows or shrinks to fit");
    installBrowserSurface();
    const location = window.location;
    window.history.replaceState = (_state, _title, url) => {
      location.search = url.slice(url.indexOf("?") === -1 ? url.length : url.indexOf("?"));
    };
    const stack = createPaneStack();
    for (const kind of ["logs", "usage", "decisions"]) stack.openPane({ kind });
    stack.noteRailWidth(1920);
    stack.enterSession(SESSION);
    const before = widths(stack);

    stack.setGroupSize("logs", before.logs + 180);
    const after = widths(stack);
    assert.equal(after.logs, before.logs + 180);
    assert.deepEqual({ ...after, logs: before.logs }, before);

    stack.noteRailWidth(1200);
    assert.deepEqual(widths(stack), after, "a narrower window scrolls the rail instead");

    stack.persistActiveDesk();
    stack.writeQuery(0);
    const reloaded = createPaneStack();
    reloaded.hydrateFromQuery();
    reloaded.noteRailWidth(3400);
    reloaded.enterSession(SESSION);
    assert.deepEqual(widths(reloaded), after, "a reload on a wider window restores every width");
  });

  it("fits the window once on a first visit, through the flexible column", () => {
    installBrowserSurface();
    const stack = createPaneStack();
    stack.noteRailWidth(2400);
    stack.enterSession(SESSION);
    const { graph, chat, usage } = widths(stack);
    assert.equal(graph + chat + usage + 2 * SPINE_SIZE, 2400, "the folded logs and latency count as spines");

    stack.closePane("chat");
    stack.noteRailWidth(3400);
    assert.equal(widths(stack).graph, graph, "and never again");
  });

  /* The old default's first-visit fill left the state flow 1036px wide in storage, and
     brought back on a reload that pushed every new column off the right edge. */
  it("fits a returning visit too, ignoring desks saved before the default changed", () => {
    installBrowserSurface();
    const old = [
      [{ id: "graph", collapsed: false, pinned: false, size: 1036 }],
      [{ id: "chat", collapsed: false, pinned: false, size: 404 }]
    ];
    localStorage.setItem(
      "temporal-agent-ui.desks.v1",
      JSON.stringify({ [SESSION]: { desk: old, savedAt: Date.now() } })
    );
    const stack = createPaneStack();
    stack.noteRailWidth(1440);
    stack.enterSession(SESSION);
    const fitted = widths(stack);
    assert.equal(fitted.graph + fitted.chat + fitted.usage + 2 * SPINE_SIZE, 1440);
  });

  it("narrows the flexible column to fit a laptop, but never below its minimum", () => {
    installBrowserSurface();
    const laptop = createPaneStack();
    laptop.noteRailWidth(1440);
    laptop.enterSession(SESSION);
    const fitted = widths(laptop);
    assert.equal(fitted.graph + fitted.chat + fitted.usage + 2 * SPINE_SIZE, 1440, "the whole default desk on screen");
    assert.equal(fitted.chat, PANE_META.chat.defaultSize, "only the state flow gives up width");

    delete globalThis.window;
    installBrowserSurface();
    const narrow = createPaneStack();
    narrow.noteRailWidth(900);
    narrow.enterSession("wf-narrow");
    assert.equal(widths(narrow).graph, PANE_META.graph.minSize, "a narrower window scrolls the rest");
  });
});
