// A scripted Strands model for the Strands integration tests. Each `stream()` call plays the next
// step of its script: a text reply, a tool call, or an echo of the tool result it was sent. Every
// call streams a reasoning fragment first and ends with usage, as a real provider does.

import { Model } from "@strands-agents/sdk";
import type {
  BaseModelConfig,
  Message,
  ModelStreamEvent,
  StreamOptions,
} from "@strands-agents/sdk";

/** A script step: reply with text, call a tool, or reply with the last tool result's text. */
export type Step = string | { tool: string; toolUseId: string; input: object } | typeof ECHO;

/** Replies with exactly the text of the last tool result the model was sent. */
export const ECHO = Symbol("echo the last tool result");

/** The model id the worker model reports, so the bracket events name it. */
export const SCRIPTED_MODEL_ID = "scripted-strands-model";

export const USAGE = { inputTokens: 10, outputTokens: 3, totalTokens: 13, cacheReadInputTokens: 4 };

export class ScriptedModel extends Model<BaseModelConfig> {
  readonly #steps: Step[];
  #next = 0;

  constructor(steps: Step[]) {
    super();
    this.#steps = steps;
  }

  override updateConfig(_config: BaseModelConfig): void {}

  override getConfig(): BaseModelConfig {
    return { modelId: SCRIPTED_MODEL_ID };
  }

  override async *stream(
    messages: Message[],
    _options?: StreamOptions,
  ): AsyncIterable<ModelStreamEvent> {
    const step = this.#steps[this.#next++];
    if (step === undefined) throw new Error("ScriptedModel script exhausted");
    yield { type: "modelMessageStartEvent", role: "assistant" };
    yield {
      type: "modelContentBlockDeltaEvent",
      delta: { type: "reasoningContentDelta", text: "Thinking." },
    };
    yield { type: "modelContentBlockStopEvent" };
    if (typeof step === "object") {
      yield {
        type: "modelContentBlockStartEvent",
        start: { type: "toolUseStart", name: step.tool, toolUseId: step.toolUseId },
      };
      yield {
        type: "modelContentBlockDeltaEvent",
        delta: { type: "toolUseInputDelta", input: JSON.stringify(step.input) },
      };
      yield { type: "modelContentBlockStopEvent" };
      yield { type: "modelMessageStopEvent", stopReason: "toolUse" };
    } else {
      const text = step === ECHO ? lastToolResultText(messages) : step;
      yield { type: "modelContentBlockStartEvent" };
      yield { type: "modelContentBlockDeltaEvent", delta: { type: "textDelta", text } };
      yield { type: "modelContentBlockStopEvent" };
      yield { type: "modelMessageStopEvent", stopReason: "endTurn" };
    }
    yield { type: "modelMetadataEvent", usage: USAGE };
  }
}

function lastToolResultText(messages: Message[]): string {
  for (const block of messages.at(-1)?.content ?? [])
    if (block.type === "toolResultBlock")
      return block.content.map((c) => (c.type === "textBlock" ? c.text : "")).join("");
  throw new Error("the last message carries no tool result");
}
