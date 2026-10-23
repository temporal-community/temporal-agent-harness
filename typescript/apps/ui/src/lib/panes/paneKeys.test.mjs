// ABOUTME: A pane id is open once across the rail and the drawer. Restoring a stored desk, a link,
// or the default drawer must not put the same id in two places, or the minimap's keyed each throws
// each_key_duplicate.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { afterEach, describe, it } from "vitest";

import { installBrowserSurface } from "../../../tests/support/controllerHarness.mjs";
import { writeStoredDesk } from "../state/agentRunStorage.ts";
import { createPaneStack, dockFocusedPane, openDefaultDrawer } from "../state/paneStack.svelte.ts";
import PaneMinimap from "./PaneMinimap.svelte";

const allIds = (...stacks) => stacks.flatMap((stack) => stack.panes.map((pane) => pane.id));
const stored = (id) => ({ id, collapsed: false, pinned: false, size: null });

function boot(search = "") {
  window.location.search = search;
  const rail = createPaneStack();
  rail.hydrateFromQuery();
  const drawer = createPaneStack({ queryPrefix: "2", initial: [], single: true, peer: rail });
  drawer.hydrateFromQuery();
  openDefaultDrawer(drawer, search);
  return { rail, drawer };
}

function assertUnique(rail, drawer) {
  const ids = allIds(rail, drawer);
  assert.deepEqual(ids, [...new Set(ids)], `duplicate pane id in ${ids.join(", ")}`);
  const body = render(PaneMinimap, {
    props: { stack: rail, drawer, describe: (pane) => ({ title: pane.kind }) }
  }).body;
  const ticks = [...body.matchAll(/data-pane-tick="([^"]+)"/g)].map(([, id]) => id);
  assert.deepEqual(ticks, [...new Set(ticks)]);
}

describe("pane ids across the rail and the drawer", () => {
  afterEach(() => {
    delete globalThis.window;
    delete globalThis.localStorage;
    delete globalThis.sessionStorage;
  });

  it("drops a stored timeline the default drawer already holds, and repeats inside the desk", () => {
    installBrowserSurface();
    writeStoredDesk("wf-b", [
      [stored("graph")],
      [stored("timeline")],
      [stored("chat")],
      [stored("logs"), stored("timeline")],
      [stored("graph")]
    ]);
    const { rail, drawer } = boot();
    assert.deepEqual(allIds(drawer), ["timeline"]);
    rail.enterSession("wf-a");
    rail.enterSession("wf-b");
    assert.deepEqual(allIds(rail), ["graph", "chat", "logs"]);
    assertUnique(rail, drawer);
  });

  it("keeps the rail's copy when a link names the same pane for both", () => {
    installBrowserSurface();
    const { rail, drawer } = boot("?p=graph,timeline&p2=timeline");
    assert.deepEqual(allIds(rail), ["graph", "timeline"]);
    assert.deepEqual(allIds(drawer), []);
    assertUnique(rail, drawer);
  });

  it("moves a pane opened on one stack while the other holds it, so docking cannot evict a second copy", () => {
    installBrowserSurface();
    const { rail, drawer } = boot();
    rail.openPane({ kind: "timeline" }, "graph");
    assert.deepEqual(allIds(drawer), []);
    assert.deepEqual(allIds(rail), ["graph", "timeline", "chat"]);
    assert.equal(rail.focusedId, "timeline");

    drawer.openPane({ kind: "timeline" });
    assert.deepEqual(allIds(rail), ["graph", "chat"]);
    assert.deepEqual(allIds(drawer), ["timeline"]);

    rail.focusPane("chat");
    dockFocusedPane(rail, drawer);
    assert.deepEqual(allIds(drawer), ["chat"]);
    assertUnique(rail, drawer);
  });
});
