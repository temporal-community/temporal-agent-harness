// ABOUTME: Keeps reload and remount motionless until snapshot catch-up and graph layout settle.
// User-opened panes and later graph changes retain their motion after this shared gate opens.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { knownFitViewport, withFitBound } from "../src/lib/components/flow/AutoFitView.svelte";

const read = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const app = read("App.svelte");
const flow = read("lib/components/flow/AgentStateFlow.svelte");
const autoFit = read("lib/components/flow/AutoFitView.svelte");
const minimap = read("lib/panes/PaneMinimap.svelte");

describe("first paint motion", () => {
  it("gates entrances until snapshot catch-up is complete", () => {
    assert.match(app, /class:motion-ready=\{motionReady\}/);
    assert.match(app, /\.app:not\(\.motion-ready\) :global\(\*:not\(\.minimap, \.minimap \*\)\)[\s\S]*transition: none !important;[\s\S]*animation: none !important;/);
    assert.match(app, /ready=\{motionReady\}/);
  });

  it("opens the gate on a session whose attach the server holds open", () => {
    /* A turn parked on approvals keeps `connecting` true indefinitely, and the
       graph stays hidden until this gate opens. */
    assert.doesNotMatch(app, /if \(booting \|\| run\.connecting/);
    assert.match(app, /setTimeout\(open, run\.connecting \? catchUpCeilingMs : 0\)/);
  });

  it("keeps the pane minimap proportional outside the motion gate", () => {
    assert.match(minimap, /\.mark \{[\s\S]*width: calc\(100% - 1px\);[\s\S]*height: var\(--gap-lg\);/);
    assert.match(minimap, /\.tick\.folded \.mark \{\s*height: var\(--gap-sm\);/);
    assert.doesNotMatch(app, /\.app:not\(\.motion-ready\) :global\(\*\),/);
  });

  it("settles the graph before enabling viewport, node, and edge motion", () => {
    assert.match(flow, /!ready \|\| !settled \|\| reduceMotion\(\)/);
    assert.match(flow, /edge\.animated \? \{ \.\.\.edge, animated: false \} : edge/);
    assert.match(flow, /\.flow-wrap:not\(\.settled\) :global\(\.svelte-flow__viewport \*\),\s*\.flow-wrap:not\(\.settled\) :global\(\.svelte-flow__minimap-node\) \{\s*visibility: hidden !important;/);
    assert.match(autoFit, /knownFitViewport\(graphNodes, store\.width, store\.height, fitViewOptions\)/);
    assert.match(flow, /graphNodes=\{graph\.nodes\}/);
    assert.match(autoFit, /const alone = settled &&/);
    assert.match(autoFit, /duration: alone \? 120 : 0/);
    assert.match(autoFit, /settled = true;\s*onSettled\(\);/);
  });

  it("settles a viewport promise that never resolves", async () => {
    const result = await withFitBound(new Promise(() => {}));
    assert.equal(result, true);
  });

  it("frames the graph from its known sizes, before anything is measured", () => {
    const nodes = [
      { id: "a", position: { x: 0, y: 0 }, width: 200, height: 100, data: {} },
      { id: "b", position: { x: 800, y: 300 }, width: 200, height: 100, data: {} }
    ];
    const viewport = knownFitViewport(nodes, 1000, 500, { padding: 0, minZoom: 0.04, maxZoom: 1 });
    assert.deepEqual(viewport, { x: 0, y: 50, zoom: 1 });
  });

  it("caps the fit at 1:1 and waits for a pane with a size", () => {
    const nodes = [{ id: "a", position: { x: 0, y: 0 }, width: 230, height: 130, data: {} }];
    assert.equal(knownFitViewport(nodes, 1000, 500, { padding: 0.16, minZoom: 0.04, maxZoom: 1 })?.zoom, 1);
    assert.equal(knownFitViewport(nodes, 0, 500, { padding: 0.16 }), null);
    assert.equal(knownFitViewport([], 1000, 500, { padding: 0.16 }), null);
  });
});
