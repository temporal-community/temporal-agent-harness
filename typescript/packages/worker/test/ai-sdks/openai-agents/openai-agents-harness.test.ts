// OpenAIStreamObserver in isolation (port of tests/ai_sdks/openai_agents/test_stream_observer.py):
// scripted agents-core stream events through the observer, publishing to an in-memory sink, and
// the exact harness events it publishes, in order.

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { StreamEvent } from "@openai/agents-core";
import type { Duration } from "@temporalio/common";
import type {
  AgentStreamItem,
  TurnStreamContext,
} from "@temporalio/agent-harness/harness/agent-protocol";
import {
  OpenAIStreamObserver,
  harnessObserverFactory,
  type OpenTurnPublisher,
} from "../../../src/ai-sdks/openai-agents-harness.ts";
import { observeStream } from "../../../src/ai-sdks/integration-helpers/stream-observer.ts";
import {
  annotationAdded,
  argsDelta,
  argsDone,
  fnCallAdded,
  functionCallItem,
  reasoningDelta,
  responseDone,
  textDelta,
} from "./scripted-model.ts";

const context = (turn_id: string): TurnStreamContext => ({
  turn_id,
  turn_number: 1,
  message_id: null,
  agent_id: "agent-abc",
});

/** An in-memory turn publisher: what was published, and what each open was given. */
function sink() {
  const events: AgentStreamItem[] = [];
  const opened: { context: TurnStreamContext; options: { batchInterval?: Duration } }[] = [];
  let disposed = 0;
  const openPublisher: OpenTurnPublisher = (ctx, options) => {
    opened.push({ context: ctx, options });
    return {
      publish: (event) => events.push(event),
      flush: async () => {},
      [Symbol.asyncDispose]: async () => {
        disposed += 1;
      },
    };
  };
  return { events, opened, openPublisher, disposed: () => disposed };
}

async function* streamOf(events: StreamEvent[], failure?: Error): AsyncIterable<StreamEvent> {
  yield* events;
  if (failure) throw failure;
}

const usage = {
  input_tokens: 11,
  output_tokens: 22,
  total_tokens: 33,
  cached_tokens: 5,
  thought_tokens: 7,
  tool_use_tokens: null,
};

describe("OpenAIStreamObserver", () => {
  it(
    "translates a full call into the bracket, reply, thought, annotation and one tool_requested",
    async () => {
      const out = sink();
      const observer = new OpenAIStreamObserver(context("t-1"), {
        model: "gpt-5.1",
        openPublisher: out.openPublisher,
      });
      await observeStream(
        observer,
        streamOf([
          { type: "response_started" },
          textDelta("Hel"),
          textDelta("lo"),
          reasoningDelta("thinking..."),
          annotationAdded(),
          fnCallAdded("fc_item_1", "call_XYZ", "lookup"),
          argsDelta("fc_item_1", '{"q":'),
          argsDelta("fc_item_1", ' "cats"}'),
          argsDone("fc_item_1", "lookup", '{"q": "cats"}'),
          responseDone([functionCallItem("call_XYZ", "lookup", '{"q": "cats"}')]),
        ]),
      );
      assert.deepStrictEqual(out.events, [
        { type: "model_interaction_started", model: "gpt-5.1" },
        { type: "reply_delta", text: "Hel" },
        { type: "reply_delta", text: "lo" },
        {
          type: "thought_summary",
          delta: {
            type: "response.reasoning_summary_text.delta",
            delta: "thinking...",
            item_id: "rs_1",
          },
        },
        {
          type: "text_annotation",
          delta: {
            type: "response.output_text.annotation.added",
            annotation: { type: "url_citation", url: "https://example.com" },
            item_id: "msg_1",
          },
        },
        {
          type: "tool_requested",
          tool_id: "call_XYZ",
          tool_name: "lookup",
          tool_input: { q: "cats" },
        },
        { type: "model_interaction_ended", model: "gpt-5.1", usage },
      ]);
      assert.equal(out.disposed(), 1);
    },
  );

  it(
    "assembles tool_requested from the streamed fragments when the done event carries no arguments",
    async () => {
      const out = sink();
      await observeStream(
        new OpenAIStreamObserver(context("t-2"), { openPublisher: out.openPublisher }),
        streamOf([
          fnCallAdded("fc_item_9", "call_BUF", "search"),
          argsDelta("fc_item_9", '{"n": '),
          argsDelta("fc_item_9", "42}"),
          argsDone("fc_item_9", "search", ""),
          responseDone([functionCallItem("call_BUF", "search", '{"n": 42}')]),
        ]),
      );
      const requested = out.events.filter((e) => e.type === "tool_requested");
      assert.deepStrictEqual(requested, [
        { type: "tool_requested", tool_id: "call_BUF", tool_name: "search", tool_input: { n: 42 } },
      ]);
    },
  );

  it("publishes model_interaction_started at dispatch, before any event arrives", async () => {
    const out = sink();
    const observer = new OpenAIStreamObserver(context("t-4"), {
      model: "gpt-5.1",
      openPublisher: out.openPublisher,
    });
    await observer.start();
    assert.deepStrictEqual(out.events, [{ type: "model_interaction_started", model: "gpt-5.1" }]);
    observer.onEvent(textDelta("hi"));
    await observer.finish();
    assert.equal(out.events.at(-1)?.type, "model_interaction_ended");
  });

  it("closes the bracket with no usage when the call streamed nothing", async () => {
    const out = sink();
    await observeStream(
      new OpenAIStreamObserver(context("t-3"), { openPublisher: out.openPublisher }),
      streamOf([]),
    );
    assert.deepStrictEqual(out.events, [
      { type: "model_interaction_started", model: null },
      { type: "model_interaction_ended", model: null, usage: null },
    ]);
  });

  it("closes the bracket and rethrows when the stream fails mid-call", async () => {
    const out = sink();
    const failure = new Error("connection reset");
    await assert.rejects(
      observeStream(
        new OpenAIStreamObserver(context("t-7"), {
          model: "gpt-5.1",
          openPublisher: out.openPublisher,
        }),
        streamOf([textDelta("partial")], failure),
      ),
      failure,
    );
    assert.deepStrictEqual(out.events, [
      { type: "model_interaction_started", model: "gpt-5.1" },
      { type: "reply_delta", text: "partial" },
      { type: "model_interaction_ended", model: "gpt-5.1", usage: null },
    ]);
    assert.equal(out.disposed(), 1);
  });

  it(
    "requests a function call that streamed no Responses events from the completed response",
    async () => {
      // A Chat Completions model streams no function_call_arguments events; its calls arrive only
      // on response_done.
      const out = sink();
      await observeStream(
        new OpenAIStreamObserver(context("t-8"), { openPublisher: out.openPublisher }),
        streamOf([responseDone([functionCallItem("call_CC", "lookup", '{"q": "dogs"}')])]),
      );
      assert.deepStrictEqual(
        out.events.filter((e) => e.type === "tool_requested"),
        [
          {
            type: "tool_requested",
            tool_id: "call_CC",
            tool_name: "lookup",
            tool_input: { q: "dogs" },
          },
        ],
      );
    },
  );

  it("treats malformed function-call arguments as an empty input", async () => {
    const out = sink();
    await observeStream(
      new OpenAIStreamObserver(context("t-9"), { openPublisher: out.openPublisher }),
      streamOf([fnCallAdded("fc", "call_BAD", "lookup"), argsDone("fc", "lookup", "[1, 2")]),
    );
    assert.deepStrictEqual(
      out.events.filter((e) => e.type === "tool_requested"),
      [{ type: "tool_requested", tool_id: "call_BAD", tool_name: "lookup", tool_input: {} }],
    );
  });
});

describe("flush cadence", () => {
  it("opens the turn's publisher with the observer's batch interval", async () => {
    const out = sink();
    const observer = new OpenAIStreamObserver(context("t-5"), {
      batchInterval: "750 milliseconds",
      openPublisher: out.openPublisher,
    });
    await observer.start();
    await observer.finish();
    assert.deepStrictEqual(out.opened, [
      { context: context("t-5"), options: { batchInterval: "750 milliseconds" } },
    ]);
  });

  it("defers to the harness default publish interval when given none", async () => {
    const out = sink();
    const observer = new OpenAIStreamObserver(context("t-6"), { openPublisher: out.openPublisher });
    await observer.start();
    await observer.finish();
    assert.deepStrictEqual(out.opened, [{ context: context("t-6"), options: {} }]);
  });

  it("builds an observer for the token's turn from harnessObserverFactory", () => {
    const observer = harnessObserverFactory(
      { context: context("t-10"), model: "gpt-5.1" },
      { batchInterval: "750 milliseconds" },
    );
    assert.ok(observer instanceof OpenAIStreamObserver);
  });
});
