// ABOUTME: Asserts the Input card is coloured as `queue` only while a message is actually
// queued behind the running turn, not for the whole time a run is going. The card's tray
// is tinted with its tone, so a run-long queue colour painted every busy Input pink.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentGraph } from "./flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });

const accepted = (disposition, text, turn_number = 1) =>
  frame("message_accepted", {
    agent_id: "root",
    turn_number,
    message_id: `msg-${text}`,
    handler: "ask",
    payload: { text },
    disposition
  });

function inputTone(frames) {
  const input = buildAgentGraph(frames).nodes.find((node) => node.id === "input");
  assert.ok(input, "the graph must draw an Input card");
  return input.data.tone;
}

const RUNNING = [
  accepted("opened", "first"),
  frame("turn_started", { user_message: "first", turn_number: 1 }),
  frame("model_interaction_started", { model: "gpt" })
];

describe("the Input card's tone", () => {
  it("is neutral while a turn runs with nothing queued", () => {
    assert.equal(inputTone(RUNNING), "neutral");
  });

  it("is queue while a message waits behind the running turn", () => {
    assert.equal(inputTone([...RUNNING, accepted("queued", "second", 2)]), "queue");
  });

  it("goes back to neutral once the queued message opens its turn", () => {
    assert.equal(
      inputTone([
        ...RUNNING,
        accepted("queued", "second", 2),
        frame("message_handler_end", { output: { text: "done" } }),
        frame("turn_started", { user_message: "second", turn_number: 2 })
      ]),
      "neutral"
    );
  });
});
