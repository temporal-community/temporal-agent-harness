// InteractionConversation rebuilds each streamed Gemini reply into the steps it sends back on the
// next call, commits a reply only once its stream completes, and refuses steps it cannot carry
// (Python's test_interactions_conversation.py).

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ApplicationFailure } from "@temporalio/common";
import { InteractionConversation } from "@temporalio/agent-harness/ai-sdks/google-genai-plugin";
import { call, completed, stream, text, thought, type Event } from "./script.ts";

describe("InteractionConversation", () => {
  it("turns a function-call reply into the steps sent next", async () => {
    const conversation = new InteractionConversation();
    conversation.addUserText("Handle ticket T-1.");
    const reply = await conversation.readReply(
      stream([
        ...thought(0, "t-sig"),
        ...call(1, "call_1", "run_code", '{"script": "x = 1"}'),
        completed(),
      ]),
    );

    assert.equal(reply.text, "");
    assert.equal(reply.interactionId, "int-1");
    assert.deepStrictEqual(
      reply.functionCalls.map((c) => [c.id, c.name, c.arguments, c.signature]),
      [["call_1", "run_code", { script: "x = 1" }, "sig-call_1"]],
    );

    conversation.addFunctionResults([
      { type: "function_result", call_id: "call_1", name: "run_code", result: "ok" },
    ]);
    assert.deepStrictEqual(conversation.steps, [
      { type: "user_input", content: [{ type: "text", text: "Handle ticket T-1." }] },
      { type: "thought", signature: "t-sig" },
      {
        type: "function_call",
        id: "call_1",
        name: "run_code",
        arguments: { script: "x = 1" },
        signature: "sig-call_1",
      },
      { type: "function_result", call_id: "call_1", name: "run_code", result: "ok" },
    ]);
  });

  it("keeps a text reply as model_output", async () => {
    const conversation = new InteractionConversation();
    conversation.addUserText("Hi");
    const reply = await conversation.readReply(
      stream([...thought(0, "s"), ...text(1, "Hel", "lo"), completed()]),
    );
    assert.equal(reply.text, "Hello");
    assert.deepStrictEqual(reply.functionCalls, []);
    assert.deepStrictEqual(conversation.steps.at(-1), {
      type: "model_output",
      content: [{ type: "text", text: "Hello" }],
    });
  });

  it("leaves the conversation unchanged when a stream errors or never completes", async () => {
    const conversation = new InteractionConversation();
    conversation.addUserText("Hi");
    const before = conversation.steps;
    const error: Event = {
      event_type: "error",
      error: { message: "overloaded", code: "unavailable" },
    };
    await assert.rejects(conversation.readReply(stream([...thought(0, "s"), error])), (e) => {
      assert.ok(e instanceof ApplicationFailure);
      assert.equal(e.type, "unavailable");
      assert.match(e.message, /overloaded/);
      return true;
    });
    await assert.rejects(conversation.readReply(stream(text(0, "partial"))), {
      message: /without interaction\.completed/,
    });
    assert.deepStrictEqual(conversation.steps, before);
  });

  it("refuses, non-retryably, a step it cannot carry back", async () => {
    const conversation = new InteractionConversation();
    const search: Event = {
      event_type: "step.start",
      index: 0,
      step: { type: "google_search_call", id: "g1", arguments: { queries: ["x"] } },
    };
    await assert.rejects(conversation.readReply(stream([search, completed()])), (e) => {
      assert.ok(e instanceof ApplicationFailure);
      assert.equal(e.type, "UnsupportedInteractionStep");
      assert.equal(e.nonRetryable, true);
      assert.match(e.message, /google_search_call/);
      return true;
    });
    assert.deepStrictEqual(conversation.steps, []);
  });

  it("returns a copy from steps", () => {
    const conversation = new InteractionConversation();
    conversation.addUserText("Hi");
    conversation.steps.push({ type: "user_input", content: [] });
    assert.equal(conversation.steps.length, 1);
  });
});
