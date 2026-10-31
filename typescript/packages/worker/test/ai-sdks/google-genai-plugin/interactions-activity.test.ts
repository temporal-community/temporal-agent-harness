// The streamed Interactions activity's translation of Gemini's SSE events into turn events, as
// Python's _interactions_activity.py publishes them: the model-interaction bracket around the
// call, reply/thought/annotation deltas, one tool_requested per function call when its step stops
// (its streamed arguments merged), file_search as tool_start/tool_end, and the events returned
// verbatim.

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { Interactions } from "@google/genai";
import type { AgentStreamItem } from "@temporalio/agent-harness/harness/agent-protocol";
import {
  InteractionsStreamObserver,
  makeGeminiInteractionsCreateStreamed,
} from "../../../src/ai-sdks/google-genai-plugin/index.ts";
import type { TurnEventPublisher } from "../../../src/harness/agent-workflow.ts";
import { observeStream } from "../../../src/ai-sdks/integration-helpers/stream-observer.ts";
import { ScriptedGemini, call, completed, created, text, type Event } from "./script.ts";

const USAGE = {
  total_input_tokens: 12,
  total_output_tokens: 7,
  total_thought_tokens: 3,
  total_cached_tokens: 2,
  total_tool_use_tokens: 1,
  total_tokens: 25,
  input_tokens_by_modality: [{ modality: "text", tokens: 12 }],
};

function recordingPublisher() {
  const published: AgentStreamItem[] = [];
  let disposed = 0;
  const publisher: TurnEventPublisher = {
    publish: (event) => published.push(event),
    flush: async () => {},
    [Symbol.asyncDispose]: async () => {
      disposed += 1;
    },
  };
  return { published, publisher, disposed: () => disposed };
}

async function* failing(events: readonly Event[], error: Error): AsyncGenerator<Event> {
  yield* events;
  throw error;
}

describe("the interactions activity's stream translation", () => {
  it("publishes Python's harness events, in order, for a scripted stream", async () => {
    const annotationDelta: Interactions.StepDelta.TextAnnotationDelta = {
      type: "text_annotation_delta",
      annotations: [{ type: "file_citation", document_uri: "doc-1", start_index: 0, end_index: 5 }],
    };
    const events: Event[] = [
      created(),
      { event_type: "step.start", index: 0, step: { type: "thought" } },
      {
        event_type: "step.delta",
        index: 0,
        delta: { type: "thought_summary", content: { type: "text", text: "Considering." } },
      },
      { event_type: "step.delta", index: 0, delta: { type: "thought_signature", signature: "s" } },
      { event_type: "step.stop", index: 0 },
      { event_type: "step.start", index: 1, step: { type: "model_output" } },
      { event_type: "step.delta", index: 1, delta: { type: "text", text: "Hel" } },
      { event_type: "step.delta", index: 1, delta: { type: "text", text: "" } },
      { event_type: "step.delta", index: 1, delta: { type: "text", text: "lo" } },
      { event_type: "step.delta", index: 1, delta: annotationDelta },
      { event_type: "step.stop", index: 1 },
      ...call(2, "call_1", "get_weather", '{"city": "Paris"}'),
      { event_type: "step.start", index: 3, step: { type: "file_search_call", id: "fs_1" } },
      { event_type: "step.delta", index: 3, delta: { type: "file_search_call" } },
      { event_type: "step.stop", index: 3 },
      { event_type: "step.start", index: 4, step: { type: "file_search_result", call_id: "fs_1" } },
      { event_type: "step.stop", index: 4 },
      {
        event_type: "step.start",
        index: 5,
        step: { type: "function_call", id: "call_2", name: "lookup", arguments: { id: 7 } },
      },
      { event_type: "step.stop", index: 5 },
      ...call(6, "call_3", "lookup", '{"id": '),
      { event_type: "error", error: { code: "unavailable", message: "ignored by the activity" } },
      completed("int-1", USAGE),
    ];
    const { published, publisher, disposed } = recordingPublisher();
    const observer = new InteractionsStreamObserver("gemini-test", () => publisher);
    await observeStream(
      observer,
      (async function* () {
        yield* events;
      })(),
    );

    assert.deepStrictEqual(published, [
      { type: "model_interaction_started", model: "gemini-test" },
      {
        type: "thought_summary",
        delta: { type: "thought_summary", content: { type: "text", text: "Considering." } },
      },
      { type: "reply_delta", text: "Hel" },
      { type: "reply_delta", text: "lo" },
      { type: "text_annotation", delta: annotationDelta },
      {
        type: "tool_requested",
        tool_id: "call_1",
        tool_name: "get_weather",
        tool_input: { city: "Paris" },
      },
      { type: "tool_start", tool_id: "fs_1", tool_name: "file_search", tool_input: {} },
      { type: "tool_end", tool_id: "fs_1", tool_name: "file_search", tool_output: null },
      { type: "tool_requested", tool_id: "call_2", tool_name: "lookup", tool_input: { id: 7 } },
      // Malformed streamed arguments are shown as {} rather than failing the activity.
      { type: "tool_requested", tool_id: "call_3", tool_name: "lookup", tool_input: {} },
      {
        type: "model_interaction_ended",
        model: "gemini-test",
        usage: {
          input_tokens: 12,
          output_tokens: 7,
          thought_tokens: 3,
          cached_tokens: 2,
          tool_use_tokens: 1,
          total_tokens: 25,
        },
      },
    ]);
    assert.equal(disposed(), 1);
  });

  it("closes the model interaction without usage when the stream fails mid-way", async () => {
    const { published, publisher, disposed } = recordingPublisher();
    const observer = new InteractionsStreamObserver("gemini-test", () => publisher);
    const broken = new Error("connection reset");
    await assert.rejects(
      observeStream(observer, failing([created(), ...text(0, "Hi")], broken)),
      broken,
    );
    assert.deepStrictEqual(published, [
      { type: "model_interaction_started", model: "gemini-test" },
      { type: "reply_delta", text: "Hi" },
      { type: "model_interaction_ended", model: "gemini-test", usage: null },
    ]);
    assert.equal(disposed(), 1);
  });

  it("returns every event verbatim and calls the SDK with its retries off", async () => {
    const events = [created(), ...text(0, "Hi"), completed()];
    const gemini = new ScriptedGemini([events]);
    const activity = makeGeminiInteractionsCreateStreamed(gemini);
    const result = await activity({ model: "gemini-test", input: "Hi", stream: true }, null);
    assert.deepStrictEqual(result, { events });
    assert.deepStrictEqual(gemini.requests, [
      {
        params: { model: "gemini-test", input: "Hi", stream: true },
        options: { retries: { strategy: "none" } },
      },
    ]);
  });
});
