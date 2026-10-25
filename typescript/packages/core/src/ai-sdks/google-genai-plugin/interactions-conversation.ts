// A Gemini Interactions conversation that the workflow holds and sends whole on every call
// (Python's _interactions_conversation.py).
//
// Why not chain with `previous_interaction_id`: the API rebuilds a chained conversation from the
// stored interactions, and a STREAMED interaction stores only its own steps. The rebuild reads the
// previous interaction and the one before it, no further, so once two streamed interactions follow
// each other the rebuilt conversation starts partway through, at a function result whose call is
// missing, and the API rejects the next function result with 400 "Please ensure that function
// response turn comes immediately after a function call turn". A tool-calling loop that streams
// every model call hits that on its third tool result.
//
// So the conversation lives here, in workflow state, and goes out as the `input` of every
// `interactions.create`. That also makes the workflow the record of what the model was told: a
// replay, a retry or a restarted worker sends exactly the same conversation. The cost is that
// every model call carries the whole conversation in its activity input.
//
// A typical tool-calling turn:
//
//   conversation.addUserText(text);
//   while (true) {
//     const stream = await gemini.interactions.create({
//       model, input: conversation.steps, tools, stream: true,
//     });
//     const reply = await conversation.readReply(stream);
//     if (reply.functionCalls.length === 0) return reply.text;
//     conversation.addFunctionResults(await runCalls(reply.functionCalls));
//   }

import type { Interactions } from "@google/genai";
import { ApplicationFailure } from "@temporalio/workflow";

/** A function call the model made. The API sends a `signature` the SDK's type does not declare. */
export type FunctionCallStep = Interactions.FunctionCallStep & { signature?: string };

/** The result of one function call, to send back with `addFunctionResults`. */
export type FunctionResultStepParam = Interactions.FunctionResultStep & { signature?: string };

type TextContentParam = { type: "text"; text: string };
type ThoughtStepParam = { type: "thought"; signature?: string; summary?: TextContentParam[] };

/** One model reply: its text, the function calls it made, and the interaction's id. */
export interface InteractionReply {
  text: string;
  functionCalls: FunctionCallStep[];
  interactionId: string;
}

/**
 * The steps of one conversation with a Gemini model, oldest first. Pass `steps` as the `input` of
 * every `interactions.create`, without `previous_interaction_id`.
 */
export class InteractionConversation {
  readonly #steps: Interactions.Step[] = [];

  /** The whole conversation, to send as `input`. */
  get steps(): Interactions.Step[] {
    return [...this.#steps];
  }

  addUserText(text: string): void {
    this.#steps.push({ type: "user_input", content: [{ type: "text", text }] });
  }

  /** The results of the function calls in the last reply, in any order. */
  addFunctionResults(results: Iterable<FunctionResultStepParam>): void {
    for (const result of results) this.#steps.push({ ...result });
  }

  /**
   * Read one streamed `interactions.create` to its end and add the model's steps.
   *
   * The steps are added only once the stream completes, so a failed call leaves the conversation
   * as it was. Throws an ApplicationFailure on an error event, on a stream that ends without
   * completing, and (non-retryably) on a step this conversation cannot carry back to the model.
   */
  async readReply(
    stream: AsyncIterable<Interactions.InteractionSSEEvent>,
  ): Promise<InteractionReply> {
    const thoughts = new Map<number, ThoughtStepParam>();
    const calls = new Map<number, FunctionCallStep>();
    const args = new Map<number, string>();
    const outputs = new Map<number, string>();
    let interactionId: string | null = null;
    for await (const event of stream) {
      switch (event.event_type) {
        case "error":
          throw ApplicationFailure.create({
            type: event.error?.code || "stream_error",
            message: event.error?.message || "stream error",
          });
        case "step.start": {
          const { index, step } = event;
          if (step.type === "thought") {
            const thought: ThoughtStepParam = { type: "thought" };
            if (step.signature) thought.signature = step.signature;
            if (step.summary?.length) thought.summary = step.summary.map(textParam);
            thoughts.set(index, thought);
          } else if (step.type === "function_call") {
            calls.set(index, step);
          } else if (step.type === "model_output") {
            outputs.set(index, (step.content ?? []).map((c) => textParam(c).text).join(""));
          } else {
            throw unsupported(`produced a '${step.type}' step`);
          }
          break;
        }
        case "step.delta": {
          const { index, delta } = event;
          const thought = thoughts.get(index);
          if (delta.type === "text" && outputs.has(index)) {
            outputs.set(index, outputs.get(index)! + (delta.text ?? ""));
          } else if (delta.type === "arguments_delta" && calls.has(index)) {
            args.set(index, (args.get(index) ?? "") + (delta.arguments ?? ""));
          } else if (delta.type === "thought_signature" && thought) {
            if (delta.signature) thought.signature = delta.signature;
          } else if (delta.type === "thought_summary" && thought) {
            if (delta.content !== undefined)
              thought.summary = [...(thought.summary ?? []), textParam(delta.content)];
          } else {
            throw unsupported(`streamed a '${delta.type}' delta`);
          }
          break;
        }
        case "interaction.completed":
          interactionId = event.interaction.id;
          break;
      }
    }
    if (interactionId === null)
      throw ApplicationFailure.create({
        type: "stream_error",
        message: "stream ended without interaction.completed event",
      });

    const modelSteps: Interactions.Step[] = [];
    const functionCalls: FunctionCallStep[] = [];
    const indexes = [...new Set([...thoughts.keys(), ...calls.keys(), ...outputs.keys()])];
    for (const index of indexes.sort((a, b) => a - b)) {
      const thought = thoughts.get(index);
      let call = calls.get(index);
      if (thought) {
        modelSteps.push(thought);
      } else if (call) {
        const raw = args.get(index);
        if (raw !== undefined) call = { ...call, arguments: parseArguments(raw) };
        functionCalls.push(call);
        const callStep: FunctionCallStep = {
          type: "function_call",
          id: call.id,
          name: call.name,
          arguments: call.arguments,
        };
        if (call.signature) callStep.signature = call.signature;
        modelSteps.push(callStep);
      } else {
        const text = outputs.get(index)!;
        modelSteps.push(
          text
            ? { type: "model_output", content: [{ type: "text", text }] }
            : { type: "model_output" },
        );
      }
    }
    this.#steps.push(...modelSteps);
    const text = [...outputs.entries()]
      .sort(([a], [b]) => a - b)
      .map(([, t]) => t)
      .join("");
    return { text, functionCalls, interactionId };
  }
}

/** A text block the conversation can send back; anything else is refused. */
function textParam(content: Interactions.Content): TextContentParam {
  if (content.type !== "text" || content.annotations?.length)
    throw unsupported(`produced ${content.type} content`);
  return { type: "text", text: content.text };
}

/** A function call's arguments, from the JSON fragments streamed for it. */
function parseArguments(raw: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(raw);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))
    throw new TypeError(`function call arguments are not a JSON object: ${raw}`);
  return parsed as Record<string, unknown>;
}

function unsupported(what: string): ApplicationFailure {
  return ApplicationFailure.create({
    type: "UnsupportedInteractionStep",
    nonRetryable: true,
    message: `the model ${what}, which an InteractionConversation cannot carry back to it`,
  });
}
