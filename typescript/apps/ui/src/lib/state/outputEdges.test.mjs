// ABOUTME: Asserts every edge that lands on an Output node is drawn as `edge-output`,
// whether Output sits outside the runtime (a parent agent) or inside it (a subagent),
// and that nothing else is. The subagent case is built by a different code path from
// the parent's, which is how its last leg came to be drawn as an ordinary step.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentGraph } from "./flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });

const FRAMES = [
  frame("turn_started", { user_message: "hi", turn_number: 1 }),
  frame("model_interaction_started", { model: "gpt" }),
  frame("model_interaction_ended", { model: "gpt" }),
  frame("message_handler_end", { output: { text: "done" } })
];

function edgesInto(graph, target) {
  return graph.edges.filter((edge) => edge.target === target);
}

describe("edges into Output", () => {
  for (const placement of ["external", "runtime"]) {
    it(`are edge-output when Output is ${placement}`, () => {
      const graph = buildAgentGraph(FRAMES, {
        inputPlacement: placement,
        outputPlacement: placement
      });
      const into = edgesInto(graph, "output");
      assert.ok(into.length > 0, "the graph must draw at least one edge into Output");
      for (const edge of into) {
        assert.equal(edge.class, "edge-output", `${edge.id} lands on Output`);
      }
      for (const edge of graph.edges.filter((edge) => edge.target !== "output")) {
        assert.notEqual(edge.class, "edge-output", `${edge.id} does not land on Output`);
      }
    });
  }
});
