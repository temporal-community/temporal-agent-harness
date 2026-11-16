// ABOUTME: The State Flow's minimap starts hidden, is shown and hidden from its own corner, and the
// choice survives a reload.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it, vi } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentStateFlow from "./AgentStateFlow.svelte";

let storedPrefs = {};
vi.mock("../../state/agentRunStorage.ts", () => ({
  readOperatorPrefs: () => storedPrefs,
  writeOperatorPrefs: () => {}
}));

const root = {
  id: "agent-runtime",
  type: "agentWorkflow",
  position: { x: 0, y: 0 },
  data: { title: "Parent agent runtime", state: "idle", interfaces: [] }
};

function flow(prefs) {
  storedPrefs = prefs;
  return stripComments(
    render(AgentStateFlow, {
      props: { graph: { nodes: [root], edges: [] }, ready: true, focus: false, onFocusChange: () => {} }
    }).body
  );
}

describe("the State Flow minimap toggle", () => {
  it("starts hidden, leaving a show button in the corner", () => {
    const body = flow({});
    assert.doesNotMatch(body, /svelte-flow__minimap/);
    assert.doesNotMatch(body, /minimap-toggle-panel[^"]*open/);
    assert.match(body, /aria-label="Show minimap"/);
  });

  it("shows the minimap once chosen, with its hide button pinned on it", () => {
    const body = flow({ flowMinimap: true });
    assert.match(body, /svelte-flow__minimap/);
    assert.match(body, /minimap-toggle-panel[^"]*open[\s\S]*?aria-label="Hide minimap"/);
  });

  it("keeps the toggle out of the zoom controls, away from the minimap", () => {
    const controls = flow({ flowMinimap: true }).match(/svelte-flow__controls[\s\S]*?<\/div>/)?.[0] ?? "";
    assert.ok(controls, "the controls render");
    assert.doesNotMatch(controls, /minimap/i);
  });
});
