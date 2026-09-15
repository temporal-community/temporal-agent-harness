// ABOUTME: Asserts the state bar only offers a control when there is a choice behind it. The bar is
// a switch between the documents the agents in a run declared, and Chip renders a <button> the
// moment it is handed an onclick — so a run with one state drew a button that answered a click with
// nothing, which is what code review caught. The name is still a chip, still toned, still marked
// active; it just stops claiming to be pressable. This is the check that fails when the onclick
// goes back to being unconditional, and the second case is the one that fails if someone "fixes"
// that by taking the switch away from runs that really do have more than one state.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import AgentStatePanel from "./AgentStatePanel.svelte";

const doc = (key, stateId) => ({
  key,
  stateId,
  workflowId: "wf-1",
  label: "planner",
  role: "parent",
  version: 2,
  value: { status: "idle" },
  changed: [],
  commits: 2,
  turnNumber: 1,
  problem: null
});

const html = (states) => render(AgentStatePanel, { props: { states } }).body;

describe("the registered-state bar", () => {
  it("is a plain chip when the run declared one state", () => {
    const one = html([doc("wf-1/plan", "plan")]);

    assert.doesNotMatch(one, /<button/, "a lone state name rendered as a button");
    assert.doesNotMatch(one, /aria-pressed/, "a lone state name claimed to be a toggle");
    // Still the chip it was: same shape, same tone, still the selected one.
    assert.match(one, /class="[^"]*chip[^"]*accent[^"]*active/);
    assert.match(one, />plan</);
  });

  it("is a real switch when there is another document to switch to", () => {
    const two = html([doc("wf-1/plan", "plan"), doc("wf-2/notes", "notes")]);

    assert.match(two, /<button/, "the state switch stopped being clickable");
    assert.match(two, /aria-pressed="true"/);
    assert.match(two, /aria-pressed="false"/);
  });
});
