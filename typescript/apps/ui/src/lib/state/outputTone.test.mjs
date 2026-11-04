// ABOUTME: Asserts the Output card keeps an error red after the failed turn ends. The run's
// `status` is reset to idle by turn_end, and an Output toned off it went green the moment a
// failed turn ended (the error message is reply text too) while its status still said ERROR.

import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { buildAgentGraph } from "./flowProjection.ts";

const frame = (event, data) => ({ event, data: { type: event, ...data } });

const output = (frames) => {
  const node = buildAgentGraph(frames).nodes.find((n) => n.id === "output");
  assert.ok(node, "the graph must draw an Output card");
  return node.data;
};

const STARTED = [
  frame("turn_started", { user_message: "hi", turn_number: 1 }),
  frame("model_interaction_started", { model: "gpt" })
];
const FAILED = [...STARTED, frame("message_handler_error", { message: "Activity task failed" })];

describe("the Output card", () => {
  it("is error-toned when the handler fails", () => {
    assert.equal(output(FAILED).state, "error");
    assert.equal(output(FAILED).tone, "error");
  });

  it("stays error-toned after the failed turn ends", () => {
    const ended = output([...FAILED, frame("turn_end", { turn_number: 1 })]);
    assert.equal(ended.state, "error");
    assert.equal(ended.tone, "error", "a failed reply must not turn green when the turn ends");
  });

  it("is done-toned once a reply has landed", () => {
    const done = output([
      ...STARTED,
      frame("model_interaction_ended", { model: "gpt" }),
      frame("message_handler_end", { output: { text: "done" } }),
      frame("turn_end", { turn_number: 1 })
    ]);
    assert.equal(done.tone, "done");
  });
});
