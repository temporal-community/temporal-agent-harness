// Unit-tests the Strands live translator (StrandsStreamObserver) in isolation: replays Strands
// model stream events through it with an in-memory publisher and checks the harness events it
// emits. Also checks harnessStrandsAgent's refusals, which need no worker.

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { tool as strandsTool, type ModelStreamEvent } from "@strands-agents/sdk";
import type { Duration } from "@temporalio/common";
import { z } from "zod";
import type {
  AgentStreamItem,
  TurnStreamContext,
} from "@temporalio/agent-harness/harness/agent-protocol";
import type { AgentWorkflowRunner } from "@temporalio/agent-harness/harness/agent-workflow";
import {
  asStrandsTool,
  harnessStrandsAgent,
} from "@temporalio/agent-harness/ai-sdks/strands-agents-harness";
import { observeStream } from "../../../src/ai-sdks/integration-helpers/stream-observer.ts";
import {
  StrandsStreamObserver,
  harnessObserverFactory,
} from "../../../src/ai-sdks/strands-agents-harness.ts";
import type { TurnEventPublisher } from "../../../src/harness/agent-workflow.ts";

const CONTEXT: TurnStreamContext = {
  turn_id: "t-1",
  turn_number: 1,
  message_id: null,
  agent_id: "agent-abc",
};

/** An in-memory turn publisher, and the options each open passed. */
function sink() {
  const events: AgentStreamItem[] = [];
  const opened: { batchInterval?: Duration }[] = [];
  const openPublisher = (_context: TurnStreamContext, options: { batchInterval?: Duration }) => {
    opened.push(options);
    return {
      publish: (event: AgentStreamItem) => void events.push(event),
      [Symbol.asyncDispose]: async () => {},
    } as unknown as TurnEventPublisher;
  };
  return { events, opened, openPublisher };
}

async function* from(events: ModelStreamEvent[]): AsyncIterable<ModelStreamEvent> {
  yield* events;
}

describe("StrandsStreamObserver", () => {
  it("streams a reply and reasoning inside the model-interaction bracket, with its usage", async () => {
    const { events, opened, openPublisher } = sink();
    const observer = new StrandsStreamObserver(CONTEXT, {
      model: "claude",
      batchInterval: "50 milliseconds",
      openPublisher,
    });
    await observeStream(
      observer,
      from([
        { type: "modelMessageStartEvent", role: "assistant" },
        {
          type: "modelContentBlockDeltaEvent",
          delta: { type: "reasoningContentDelta", text: "Thinking" },
        },
        {
          type: "modelContentBlockDeltaEvent",
          delta: { type: "reasoningContentDelta", signature: "sig" },
        },
        { type: "modelContentBlockDeltaEvent", delta: { type: "textDelta", text: "Hello" } },
        { type: "modelContentBlockDeltaEvent", delta: { type: "textDelta", text: ", world" } },
        { type: "modelContentBlockStopEvent" },
        { type: "modelMessageStopEvent", stopReason: "endTurn" },
        {
          type: "modelMetadataEvent",
          usage: { inputTokens: 12, outputTokens: 5, totalTokens: 17, cacheReadInputTokens: 4 },
        },
      ]),
    );

    assert.deepStrictEqual(events, [
      { type: "model_interaction_started", model: "claude" },
      { type: "thought_summary", delta: { text: "Thinking" } },
      { type: "thought_summary", delta: { signature: "sig" } },
      { type: "reply_delta", text: "Hello" },
      { type: "reply_delta", text: ", world" },
      {
        type: "model_interaction_ended",
        model: "claude",
        usage: {
          input_tokens: 12,
          output_tokens: 5,
          total_tokens: 17,
          thought_tokens: null,
          cached_tokens: 4,
          tool_use_tokens: null,
        },
      },
    ]);
    assert.deepStrictEqual(opened, [{ batchInterval: "50 milliseconds" }]);
  });

  it("publishes a tool call streamed in fragments as one tool_requested keyed by toolUseId", async () => {
    const { events, openPublisher } = sink();
    await observeStream(
      new StrandsStreamObserver(CONTEXT, { model: "claude", openPublisher }),
      from([
        {
          type: "modelContentBlockStartEvent",
          start: { type: "toolUseStart", toolUseId: "tu-1", name: "get_weather" },
        },
        {
          type: "modelContentBlockDeltaEvent",
          delta: { type: "toolUseInputDelta", input: '{"ci' },
        },
        {
          type: "modelContentBlockDeltaEvent",
          delta: { type: "toolUseInputDelta", input: 'ty": "Paris"}' },
        },
        { type: "modelContentBlockStopEvent" },
        {
          type: "modelContentBlockStartEvent",
          start: { type: "toolUseStart", toolUseId: "tu-2", name: "roll" },
        },
        { type: "modelContentBlockStopEvent" },
      ]),
    );

    assert.deepStrictEqual(
      events.filter((e) => e.type === "tool_requested"),
      [
        {
          type: "tool_requested",
          tool_id: "tu-1",
          tool_name: "get_weather",
          tool_input: { city: "Paris" },
        },
        { type: "tool_requested", tool_id: "tu-2", tool_name: "roll", tool_input: {} },
      ],
    );
  });

  it("closes the bracket when the stream fails", async () => {
    const { events, openPublisher } = sink();
    async function* failing(): AsyncIterable<ModelStreamEvent> {
      yield { type: "modelContentBlockDeltaEvent", delta: { type: "textDelta", text: "Hel" } };
      throw new Error("provider down");
    }
    await assert.rejects(
      observeStream(new StrandsStreamObserver(CONTEXT, { model: "claude", openPublisher }), failing()),
      /provider down/,
    );
    assert.deepStrictEqual(events, [
      { type: "model_interaction_started", model: "claude" },
      { type: "reply_delta", text: "Hel" },
      { type: "model_interaction_ended", model: "claude", usage: null },
    ]);
  });

  it("refuses a routing token that is not a harness stream route", () => {
    assert.throws(
      () => harnessObserverFactory("a-raw-topic", {}),
      (e: unknown) => (e as { type?: string }).type === "HarnessStreamRouteMissing",
    );
  });
});

// The refusals happen before the runner is used, and a real runner needs a workflow.
const NO_RUNNER = {} as AgentWorkflowRunner;

describe("harnessStrandsAgent", () => {
  it("refuses a Strands tool that is not a harness tool", () => {
    const roll = strandsTool({
      name: "roll",
      description: "Roll a die.",
      inputSchema: z.object({}),
      callback: () => 4,
    });
    assert.throws(
      () => harnessStrandsAgent(NO_RUNNER, { model: "m", tools: [roll] }),
      /roll is not a harness tool/,
    );
  });

  for (const key of ["streamingTopic", "streamRoute"])
    it(`refuses its own streaming setting ${key}`, () => {
      assert.throws(
        () => harnessStrandsAgent(NO_RUNNER, { model: "m", [key]: undefined } as never),
        new RegExp(key),
      );
    });

  it("refuses a callable that is not a harness tool", () => {
    assert.throws(
      () => asStrandsTool(NO_RUNNER, { name: "plain", invoke: async () => 1 } as never),
      /plain is not a harness tool/,
    );
  });
});
