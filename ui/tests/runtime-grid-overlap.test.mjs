// ABOUTME: Asserts that cards in the runtime grid never overlap each other or spill out of the
// runtime boundary. Columns used to sit at a fixed card-width pitch, so a card wider than that — a
// Code Mode host holding two host calls is 528px against a 230px pitch — ran under the card in the
// next column.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentGraph } from "$lib/state/flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });

const toolFrames = (id, name) => [
  frame("tool_requested", { tool_id: id, tool_name: name, tool_input: { q: "boston" } }),
  frame("tool_start", { tool_id: id, tool_name: name }),
  frame("tool_end", { tool_id: id, tool_name: name, tool_output: "ok" })
];

function rect(node) {
  return {
    id: node.id,
    left: node.position.x,
    top: node.position.y,
    right: node.position.x + (node.data.nodeWidth ?? 230),
    bottom: node.position.y + (node.data.nodeHeight ?? 130)
  };
}

const intersects = (a, b) =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

describe("the runtime grid", () => {
  it("places a card after a wide card without overlapping it", () => {
    const graph = buildAgentGraph([
      frame("turn_started", { user_message: "plan a trip", turn_number: 1 }),
      frame("model_interaction_started", { model: "gpt-5.1" }),
      frame("model_interaction_ended", { model: "gpt-5.1" }),
      frame("tool_requested", {
        tool_id: "host",
        tool_name: "run_flow",
        tool_input: { script: "print('hi')" }
      }),
      frame("tool_start", { tool_id: "host", tool_name: "run_flow" }),
      ...toolFrames("a", "run_agent"),
      ...toolFrames("b", "run_agent"),
      frame("tool_end", { tool_id: "host", tool_name: "run_flow", tool_output: "done" }),
      ...toolFrames("after", "search_flights")
    ]);

    const boundary = graph.nodes.find((node) => node.type === "agentWorkflow");
    assert.ok(boundary, "the fixture should draw a runtime boundary");
    /* Grid cards only: Code Mode children (z 14) are laid out inside their host. */
    const cards = graph.nodes
      .filter((node) => node.type !== "agentWorkflow" && node.zIndex === 10)
      .filter((node) => !["input", "output"].includes(node.id))
      .map(rect);
    assert.ok(
      cards.some((card) => card.right - card.left > 300),
      "the fixture should contain a card wider than one column"
    );

    for (let i = 0; i < cards.length; i += 1) {
      for (let j = i + 1; j < cards.length; j += 1) {
        assert.ok(
          !intersects(cards[i], cards[j]),
          `${cards[i].id} overlaps ${cards[j].id}: ${JSON.stringify([cards[i], cards[j]])}`
        );
      }
    }

    const right = boundary.position.x + boundary.data.boundaryWidth;
    for (const card of cards) {
      assert.ok(card.right <= right, `${card.id} spills out of the runtime boundary`);
    }
  });
});
