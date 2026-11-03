// ABOUTME: Before a run's first turn the State Flow drew the root agent's box with an empty body
// and a minimap of that one box, which read as broken. It now shows its empty state pinned over an
// empty canvas, and the box arrives with the first turn.

import assert from "node:assert/strict";
import { render } from "svelte/server";
import { describe, it } from "vitest";

import { stripComments } from "../../../../tests/support/controllerHarness.mjs";
import AgentStateFlow from "./AgentStateFlow.svelte";
import StateFlowEmpty from "./StateFlowEmpty.svelte";

const root = {
  id: "agent-runtime",
  type: "agentWorkflow",
  position: { x: 0, y: 0 },
  data: { title: "Parent agent runtime", state: "idle", interfaces: [{ name: "ask", description: "" }] }
};

function flow(runHasTurns) {
  return stripComments(
    render(AgentStateFlow, {
      props: {
        graph: { nodes: [root], edges: [] },
        ready: true,
        focus: false,
        onFocusChange: () => {},
        runHasTurns,
        agentLabel: "Parent agent",
        accepts: ["ask"]
      }
    }).body
  );
}

describe("the State Flow before its first turn", () => {
  const body = stripComments(
    render(StateFlowEmpty, { props: { agentLabel: "Parent agent", accepts: ["ask", "cancel"] } }).body
  );

  it("pins the shared empty state, with its icon, over an empty canvas", () => {
    assert.match(body, /class="empty-state[ "]/);
    assert.match(body, /<span class="empty-icon[^"]*" aria-hidden="true">[\s\S]{0,40}?<svg/);
    assert.match(body, /svelte-flow__background/);
    assert.doesNotMatch(body, /workflow-boundary|svelte-flow__minimap/);
  });

  it("names the agent and the handlers it declares", () => {
    assert.match(body, /Each turn Parent agent takes/);
    assert.match(body, /It accepts\s*<code>ask<\/code>, <code>cancel<\/code>\./);
  });

  it("replaces the root box until the run has a turn", () => {
    assert.match(flow(false), /No turns yet/);
    assert.doesNotMatch(flow(false), /flow-wrap/);
    assert.doesNotMatch(flow(true), /No turns yet/);
    assert.match(flow(true), /flow-wrap/);
  });
});
