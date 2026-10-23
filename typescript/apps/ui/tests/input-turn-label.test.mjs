// ABOUTME: Asserts the Input card names the turn of the message it shows. Jumping to a turn lands
// on the message_accepted that opened it, one frame ahead of turn_started, and the card used to show
// that message under the previous turn's label ("TURN 2" over turn 3's text).

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentTreeGraph } from "$lib/state/flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });
const accepted = (turn_number, text, disposition) =>
  frame("message_accepted", { turn_number, handler: "chat", payload: { text }, disposition });

function inputCard(frames) {
  const graph = buildAgentTreeGraph([
    { workflowId: "wf", role: "parent", label: "run", frames, agentInterface: [] }
  ]);
  return graph.nodes.find((node) => node.id.endsWith("input")).data;
}

const finished = (n) => [
  accepted(n, `message ${n}`, "opened"),
  frame("turn_started", { turn_number: n }),
  frame("message_handler_end", { turn_number: n, output: { text: `reply ${n}` } }),
  frame("turn_end", { turn_number: n })
];

describe("the Input card's turn label", () => {
  it("names the turn a message opened before its turn_started arrives", () => {
    const card = inputCard([...finished(1), ...finished(2), accepted(3, "message 3", "opened")]);
    assert.equal(card.detail, "message 3");
    assert.equal(card.state, "turn 3");
  });

  it("counts queued messages, then shows each queued turn its own message when it starts", () => {
    const running = [accepted(1, "first", "opened"), frame("turn_started", { turn_number: 1 })];
    const queue = [accepted(2, "second", "queued"), accepted(3, "third", "queued")];
    assert.equal(inputCard([...running, ...queue]).state, "2 queued");
    const second = inputCard([
      ...running,
      ...queue,
      frame("turn_end", { turn_number: 1 }),
      frame("turn_started", { turn_number: 2 })
    ]);
    assert.deepEqual([second.state, second.detail], ["turn 2", "second"]);
  });
});
