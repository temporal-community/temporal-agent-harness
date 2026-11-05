// ABOUTME: Asserts that no room ever sits empty after the last rail column, while a drag still
// changes only the dragged column: the state flow stretches over spare room from a wider window or a
// closed column, a narrower column hands its room to the last open one, the last open column stops
// at the rail's end, and a desk wider than the window keeps its widths and scrolls. Spare room used
// to sit empty, and before that the stylesheet shared it out among every column.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, describe, it } from "vitest";

import { PANE_META, SPINE_SIZE } from "../panes/registry.ts";
import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { createPaneStack, slotKey } from "./paneStack.svelte.ts";

const SESSION = "wf-own-widths";
const rail = readFileSync(new URL("../panes/PaneRail.svelte", import.meta.url), "utf8");
const folded = (group) => group.every((pane) => pane.collapsed);
const widths = (stack) =>
  Object.fromEntries(stack.groups.map((group) => [slotKey(group), stack.sizeOfGroup(group)]));
const deskWidth = (stack) =>
  stack.groups.reduce((sum, group) => sum + (folded(group) ? SPINE_SIZE : stack.sizeOfGroup(group)), 0);
const without = (record, ...keys) =>
  Object.fromEntries(Object.entries(record).filter(([key]) => !keys.includes(key)));

function deskAt(width, session = SESSION) {
  installBrowserSurface();
  const stack = createPaneStack();
  stack.noteRailWidth(width);
  stack.enterSession(session);
  return stack;
}

describe("a column's width", () => {
  afterEach(() => {
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.sessionStorage;
  });

  it("is drawn as given, and a drag changes only that column while the rest slide and the rail scrolls", () => {
    assert.match(rail, /\n {2}\.rail-slot \{[^}]*flex: 0 0 var\(--slot-size\);/, "the stylesheet fits nothing");
    const stack = deskAt(1440);
    const before = widths(stack);
    stack.setGroupSize("chat", before.chat + 200);
    assert.deepEqual(widths(stack), { ...before, chat: before.chat + 200 }, "the state flow does not take it up");
    assert.equal(deskWidth(stack), 1640, "the rail scrolls");
  });

  it("hands the room a narrower column leaves to the last open column", () => {
    const stack = deskAt(1440);
    const before = widths(stack);
    stack.setGroupSize("chat", before.chat - 50);
    assert.deepEqual(widths(stack), { ...before, chat: before.chat - 50, usage: before.usage + 50 });
    stack.setGroupSize("graph", before.graph - 80);
    assert.equal(widths(stack).chat, before.chat - 50, "the chat beside the state flow slides, it does not resize");
    assert.equal(deskWidth(stack), 1440, "nothing sits empty");
  });

  it("takes lent room back from the last open column when an overshoot is dragged back", () => {
    const stack = deskAt(1440);
    const before = widths(stack);
    stack.setGroupSize("graph", before.graph - 100);
    stack.setGroupSize("graph", before.graph - 40);
    assert.deepEqual(widths(stack), { ...before, graph: before.graph - 40, usage: before.usage + 40 }, "within a drag");
    stack.setGroupSize("graph", before.graph);
    assert.deepEqual(widths(stack), before, "back where it started, nothing scrolls");

    stack.setGroupSize("chat", before.chat - 60);
    stack.setGroupSize("graph", before.graph + 60);
    assert.equal(widths(stack).usage, before.usage, "a later drag of another column takes it back too");
    assert.equal(deskWidth(stack), 1440);
    stack.setGroupSize("graph", before.graph + 100);
    assert.equal(deskWidth(stack), 1480, "only past that does the rail scroll");
  });

  it("narrows the last open column with the state flow stretching over its room, and lets it grow past", () => {
    const stack = deskAt(1440);
    const before = widths(stack);
    stack.setGroupSize("usage", before.usage - 100);
    assert.deepEqual(widths(stack), { ...before, usage: before.usage - 100, graph: before.graph + 100 });
    assert.equal(deskWidth(stack), 1440, "nothing sits empty after the folded columns");
    stack.setGroupSize("usage", widths(stack).usage + 100);
    assert.equal(deskWidth(stack), 1540, "and growing it scrolls the rail");
  });

  it("stretches the state flow over room a wider window or a closed column leaves", () => {
    const stack = deskAt(1440);
    stack.noteRailWidth(2560);
    assert.equal(deskWidth(stack), 2560, "zoomed out");
    stack.closePane("chat");
    assert.equal(deskWidth(stack), 2560, "a column closed");
    stack.toggleCollapse("graph");
    assert.equal(deskWidth(stack), 2560, "with the state flow folded, the last open column stretches");
  });

  it("fits a first visit both ways, and keeps a sized state flow's width when the window narrows", () => {
    for (const width of [1328, 1440, 1920, 3400]) {
      assert.equal(deskWidth(deskAt(width, `wf-${width}`)), width, `at ${width}px`);
      delete globalThis.window;
    }
    const narrow = deskAt(900, "wf-narrow");
    assert.equal(widths(narrow).graph, PANE_META.graph.minSize, "past its minimum the rail scrolls");
    delete globalThis.window;

    const stack = deskAt(2560);
    const { graph } = widths(stack);
    stack.setGroupSize("graph", graph);
    stack.noteRailWidth(1440);
    assert.equal(widths(stack).graph, graph, "the reader's width holds, and the rail scrolls");
  });

  it("keeps every column's width with all of them open on a narrow window, and scrolls", () => {
    const stack = deskAt(1440);
    stack.toggleCollapse("logs");
    stack.toggleCollapse("latency");
    assert.ok(deskWidth(stack) > 1440);
    const before = widths(stack);
    stack.setGroupSize("chat", before.chat + 100);
    assert.deepEqual(widths(stack), { ...before, chat: before.chat + 100 }, "no column is squeezed to make room");
  });

  it("restores every other column's width on a reload at another window width", () => {
    installBrowserSurface();
    const location = window.location;
    window.history.replaceState = (_state, _title, url) => {
      location.search = url.slice(url.indexOf("?") === -1 ? url.length : url.indexOf("?"));
    };
    const stack = createPaneStack();
    stack.noteRailWidth(1920);
    stack.enterSession(SESSION);
    stack.setGroupSize("chat", 520);
    const after = widths(stack);
    stack.persistActiveDesk();
    stack.writeQuery(0);

    const reloaded = createPaneStack();
    reloaded.hydrateFromQuery();
    reloaded.noteRailWidth(3400);
    reloaded.enterSession(SESSION);
    assert.deepEqual(without(widths(reloaded), "graph"), without(after, "graph"));
    assert.equal(deskWidth(reloaded), 3400, "and the state flow stretches over the rest");
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
    assert.equal(deskWidth(stack), 1440);
  });

  it("narrows only the state flow to fit a laptop", () => {
    const laptop = deskAt(1440);
    assert.equal(deskWidth(laptop), 1440, "the whole default desk on screen");
    assert.equal(widths(laptop).chat, PANE_META.chat.defaultSize, "only the state flow gives up width");
  });
});
