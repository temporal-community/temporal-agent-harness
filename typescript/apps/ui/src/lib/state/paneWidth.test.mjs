// ABOUTME: Asserts that every rail column owns its width: dragging one changes that one and no
// other, and neither a window resize nor a reload changes any. Seam drags used to trade width with
// the neighbour, and before that the stylesheet shared it out among every column, while a fit
// scaled saved widths to the window.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, describe, it } from "vitest";

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

  it("fills the window once on a first visit, through the flexible column", () => {
    installBrowserSurface();
    const stack = createPaneStack();
    stack.noteRailWidth(1440);
    stack.enterSession(SESSION);
    const { graph, chat } = widths(stack);
    assert.equal(graph + chat, 1440);

    stack.closePane("chat");
    stack.noteRailWidth(2400);
    assert.equal(widths(stack).graph, graph, "and never again");
  });
});
