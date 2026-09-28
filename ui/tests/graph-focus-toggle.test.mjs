// ABOUTME: Pins the graph pane's focus toggle: pressed means focus is on, it carries no tooltip, it
// does not borrow the app's tool-call glyph, and Lucide's stroked icons are not filled solid by
// xyflow's control-svg rule.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "vitest";

const source = readFileSync(
  new URL("../src/lib/components/flow/AgentStateFlow.svelte", import.meta.url),
  "utf8"
);
const control = source.match(/<ControlButton[\s\S]*?<\/ControlButton>/)?.[0] ?? "";

describe("graph focus toggle", () => {
  it("reads pressed as focus on, with no tooltip", () => {
    assert.ok(control, "the ControlButton block is present");
    assert.match(control, /aria-label="Hide finished tool calls"/);
    assert.match(control, /aria-pressed=\{focus\}/);
    assert.doesNotMatch(control, /aria-pressed=\{!focus\}/);
    assert.doesNotMatch(control, /data-tip|title=/);
    assert.doesNotMatch(control, /<Wrench/);
  });

  it("unfills Lucide icons in controls without touching xyflow's own glyphs", () => {
    assert.match(
      source,
      /:global\(\.svelte-flow__controls-button svg\.lucide-icon\)\s*\{[^}]*fill:\s*none/
    );
    assert.doesNotMatch(source, /:global\(\.svelte-flow__controls-button svg\)\s*\{[^}]*fill:\s*none/);
  });
});
