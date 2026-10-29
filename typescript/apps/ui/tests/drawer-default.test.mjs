// ABOUTME: Guards the bottom drawer's default trace and its compact transport mode.
// Explicit drawer panes in shared URLs still take precedence over that default.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, describe, it } from "vitest";
import { toggleView } from "../src/lib/panes/launcher.ts";
import { createPaneStack, openDefaultDrawer } from "../src/lib/state/paneStack.svelte.ts";

const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");
const minimap = readFileSync(new URL("../src/lib/panes/PaneMinimap.svelte", import.meta.url), "utf8");

describe("the bottom drawer default", () => {
  afterEach(() => delete globalThis.window);

  it("opens the workflow timeline", () => {
    assert.match(app, /drawer\.groups\.length === 0\) drawer\.openPane\(\{ kind: "timeline" \}\)/);
  });

  it("uses the transport overview for either lone trace", () => {
    assert.match(app, /kind === "latency"\s*\|\|\s*drawer\.groups\[0\]\?\.\[0\]\?\.kind === "timeline"/);
    assert.match(app, /overview=\{traceOverview\}/);
  });

  it("honors an explicit latency drawer pane beside an empty main rail", () => {
    globalThis.window = { location: { pathname: "/", search: "?p=&p2=latency" } };
    const drawer = createPaneStack({ queryPrefix: "2", initial: [] });
    drawer.hydrateFromQuery();
    assert.deepEqual(drawer.groups.map((group) => group.map((pane) => pane.kind)), [["latency"]]);
  });

  it("opens state flow and agent chat on the rail and the timeline in the drawer only when the URL names no desk", () => {
    const kinds = (stack) => stack.groups.map((group) => group.map((pane) => pane.kind));
    const visit = (search) => {
      globalThis.window = { location: { pathname: "/", search } };
      const rail = createPaneStack();
      const drawer = createPaneStack({ queryPrefix: "2", initial: [], single: true });
      rail.hydrateFromQuery();
      drawer.hydrateFromQuery();
      openDefaultDrawer(drawer, search);
      return { rail, drawer };
    };

    const fresh = visit("");
    assert.deepEqual(kinds(fresh.rail), [["graph"], ["chat"]]);
    assert.deepEqual(kinds(fresh.drawer), [["timeline"]]);

    const linked = visit("?p=logs&p2=latency");
    assert.deepEqual(kinds(linked.rail), [["logs"]]);
    assert.deepEqual(kinds(linked.drawer), [["latency"]]);

    const railOnly = visit("?p=logs");
    assert.deepEqual(kinds(railOnly.rail), [["logs"]]);
    assert.deepEqual(kinds(railOnly.drawer), []);

    assert.match(app, /openDefaultDrawer\(drawer, window\.location\.search\)/);
  });
});

describe("shutting the bottom drawer", () => {
  it("folds its pane at zero height instead of closing it, and boots shut from a folded link", () => {
    assert.doesNotMatch(app, /drawer\.closePane/);
    assert.match(app, /drawer\.foldAll\(drawerHeight === 0\)/);
    assert.match(app, /const drawerBootShut = drawer\.allFolded;/);
    assert.match(app, /\$state\(drawerBootShut \? 0 :/);
  });

  it("opens again, not closes its pane, when that pane's view is picked in the minimap", () => {
    const rail = createPaneStack();
    const drawer = createPaneStack({ queryPrefix: "2", initial: [] });
    drawer.openPane({ kind: "timeline" });
    drawer.foldAll(true);
    let reopened = 0;
    const shut = { stack: drawer, reopen: () => reopened++ };

    toggleView("timeline", rail, [rail, drawer], shut);
    assert.equal(reopened, 1);
    assert.ok(drawer.has("timeline"), "the shut drawer's pane must survive the pick");

    toggleView("timeline", rail, [rail, drawer]);
    assert.ok(!drawer.has("timeline"), "an open drawer's view still closes from the launcher");

    assert.match(app, /onOpenDrawer=\{drawerHeight === 0 \? toggleDrawer : undefined\}/);
    assert.match(minimap, /toggleView\(view\.kind, stack, stacks, shut\)/);
    assert.match(minimap, /toggle: toggleAt/, "Enter and Space take the same path as a click");
  });
});
