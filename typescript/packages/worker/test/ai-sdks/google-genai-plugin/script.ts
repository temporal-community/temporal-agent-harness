// Scripted Interactions SSE events, shaped as the API streams them, and a fake client that replays
// one script per `interactions.create`.

import type { GoogleGenAIRequestOptions, Interactions } from "@google/genai";
import type { InteractionCreateParamsStreaming } from "@temporalio/agent-harness/ai-sdks/google-genai-plugin";
import type { GeminiInteractionsClient } from "../../../src/ai-sdks/google-genai-plugin/index.ts";

export type Event = Interactions.InteractionSSEEvent;

export const created = (id = "int-1"): Event => ({
  event_type: "interaction.created",
  interaction: { id, status: "in_progress" },
});

export const completed = (id = "int-1", usage?: Interactions.Usage): Event => ({
  event_type: "interaction.completed",
  interaction: { id, status: "completed", ...(usage ? { usage } : {}) },
});

export function thought(index: number, signature: string): Event[] {
  return [
    { event_type: "step.start", index, step: { type: "thought" } },
    { event_type: "step.delta", index, delta: { type: "thought_signature", signature } },
    { event_type: "step.stop", index },
  ];
}

/** A function call whose JSON arguments stream in two fragments. */
export function call(index: number, id: string, name: string, args: string): Event[] {
  const half = Math.floor(args.length / 2);
  const step = { type: "function_call", id, name, arguments: {}, signature: `sig-${id}` } as const;
  return [
    { event_type: "step.start", index, step },
    {
      event_type: "step.delta",
      index,
      delta: { type: "arguments_delta", arguments: args.slice(0, half) },
    },
    {
      event_type: "step.delta",
      index,
      delta: { type: "arguments_delta", arguments: args.slice(half) },
    },
    { event_type: "step.stop", index },
  ];
}

export function text(index: number, ...chunks: string[]): Event[] {
  return [
    { event_type: "step.start", index, step: { type: "model_output" } },
    ...chunks.map((t): Event => ({
      event_type: "step.delta",
      index,
      delta: { type: "text", text: t },
    })),
    { event_type: "step.stop", index },
  ];
}

export async function* stream(events: readonly Event[]): AsyncGenerator<Event> {
  yield* events;
}

/** A client whose n-th `interactions.create` streams `scripts[n]`, recording what it was sent. */
export class ScriptedGemini implements GeminiInteractionsClient {
  readonly requests: {
    params: InteractionCreateParamsStreaming;
    options?: GoogleGenAIRequestOptions;
  }[] = [];
  readonly interactions: GeminiInteractionsClient["interactions"];

  constructor(scripts: readonly (readonly Event[])[]) {
    this.interactions = {
      create: async (params, options) => {
        const script = scripts[this.requests.length];
        this.requests.push({ params: structuredClone(params), ...(options ? { options } : {}) });
        if (script === undefined) throw new Error(`no script for call ${this.requests.length}`);
        return stream(script);
      },
    };
  }
}
