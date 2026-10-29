// ABOUTME: Keeps reload and remount motionless until snapshot catch-up and graph layout settle.
// User-opened panes and later graph changes retain their motion after this shared gate opens.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

import { withFitBound } from "../src/lib/components/flow/AutoFitView.svelte";

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
    assert.match(flow, /\.flow-wrap:not\(\.settled\) :global\(\.svelte-flow__viewport \*\) \{\s*visibility: hidden !important;/);
    assert.match(autoFit, /const \{ fitView, getNodes \} = useSvelteFlow\(\)/);
    assert.match(autoFit, /await waitForMeasuredNodes\(request\)/);
    assert.match(autoFit, /current && current === previous/);
    assert.match(autoFit, /const alone = settled &&/);
    assert.match(autoFit, /duration: alone \? 120 : 0/);
    assert.match(autoFit, /settled = true;\s*onSettled\(\);/);
  });

  it("settles a fitView promise that never resolves", async () => {
    const result = await withFitBound(new Promise(() => {}));
    assert.equal(result, true);
  });
});
