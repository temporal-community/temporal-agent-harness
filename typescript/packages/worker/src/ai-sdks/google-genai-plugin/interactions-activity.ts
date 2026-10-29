// The worker-side activity for the Interactions API (Python's _interactions_activity.py).
//
// `gemini_interactions_create_streamed` holds the worker's real client, calls
// `interactions.create`, and returns every SSE event of the stream in order, so the workflow
// iterates them as it would the SDK's stream. Given the turn's stream context, it also translates
// the events into harness events as they arrive and publishes them to the turn from the activity.
//
// A step is one unit of the model's work in a turn (emit text, think, call a function, call or
// answer file_search), streamed as step.start (carrying the `Step`), any number of step.delta
// (carrying a `Delta`, the step's content) and step.stop (carrying only the index). A step's
// content arrives on its deltas, not on the step.start that opens it, so the translation matches
// the deltas. file_search is the exception: its call and result steps and deltas are empty
// markers, and what it retrieved surfaces as text_annotation deltas on the reply.

import type { GoogleGenAIRequestOptions, Interactions } from "@google/genai";
import type { Duration } from "@temporalio/common";
import type {
  GeminiInteractionsActivity,
  InteractionCreateParamsStreaming,
} from "@temporalio/agent-harness/ai-sdks/google-genai-plugin";
import type {
  AgentStreamItem,
  TokenUsage,
  TurnStreamContext,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { publisherFromActivity, type TurnEventPublisher } from "../../harness/agent-workflow.ts";
import {
  observeStream,
  type ObserverFactory,
  type StreamObserver,
} from "../integration-helpers/stream-observer.ts";
import { translateGeminiErrors } from "./errors.ts";

type InteractionSSEEvent = Interactions.InteractionSSEEvent;

/** The slice of `GoogleGenAI` the activity calls; a `GoogleGenAI` is one. */
export interface GeminiInteractionsClient {
  readonly interactions: {
    create(
      params: InteractionCreateParamsStreaming,
      options?: GoogleGenAIRequestOptions,
    ): Promise<AsyncIterable<InteractionSSEEvent>>;
  };
}

// The Interactions client retries 408, 409, 429 and 5xx inside each attempt; Temporal's retry
// policy owns retries instead.
const NO_SDK_RETRIES: GoogleGenAIRequestOptions = { retries: { strategy: "none" } };

/** The activity that runs one streamed `interactions.create` against `client`. */
export function makeGeminiInteractionsCreateStreamed(
  client: GeminiInteractionsClient,
): GeminiInteractionsActivity {
  return async function gemini_interactions_create_streamed(params, streamContext) {
    const events: InteractionSSEEvent[] = [];
    // A Gemini client error (4xx other than 408/429) fails the activity non-retryably, including
    // one raised mid-stream; every other failure stays retryable.
    await translateGeminiErrors(async () => {
      const stream = createStream(client, params);
      if (streamContext === null) {
        for await (const event of stream) events.push(event);
        return;
      }
      const model = "model" in params && typeof params.model === "string" ? params.model : null;
      const observer = interactionsObserver({ context: streamContext, model }, {});
      await observeStream(observer, stream, (event) => events.push(event));
    });
    return { events };
  };
}

// Lazy, so the observer's model_interaction_started is published before the API call is made.
async function* createStream(
  client: GeminiInteractionsClient,
  params: InteractionCreateParamsStreaming,
): AsyncGenerator<InteractionSSEEvent> {
  yield* await client.interactions.create({ ...params, stream: true }, NO_SDK_RETRIES);
}

/** What routes one streamed call's events: the turn to publish to and the model requested. */
export interface InteractionsStreamToken {
  context: TurnStreamContext;
  model: string | null;
}

/** A fresh observer publishing one call's events to the token's turn from the activity. */
export const interactionsObserver: ObserverFactory<InteractionsStreamToken, InteractionSSEEvent> = (
  { context, model },
  { batchInterval },
) =>
  new InteractionsStreamObserver(model, () =>
    publisherFromActivity(context, batchInterval === undefined ? {} : { batchInterval }),
  );

/**
 * Brackets one streamed call as one model interaction: model_interaction_started before the first
 * event, the translated events as they arrive, and model_interaction_ended (with the usage off
 * interaction.completed, or null if the stream failed first) after the last, even on failure.
 */
export class InteractionsStreamObserver implements StreamObserver<InteractionSSEEvent> {
  readonly #model: string | null;
  readonly #openPublisher: () => TurnEventPublisher;
  #publisher: TurnEventPublisher | null = null;
  #events: StreamEventPublisher | null = null;
  #usage: TokenUsage | null = null;

  constructor(model: string | null, openPublisher: () => TurnEventPublisher) {
    this.#model = model;
    this.#openPublisher = openPublisher;
  }

  async start(): Promise<void> {
    const publisher = this.#openPublisher();
    this.#publisher = publisher;
    this.#events = new StreamEventPublisher((event) => publisher.publish(event));
    publisher.publish({ type: "model_interaction_started", model: this.#model });
  }

  onEvent(event: InteractionSSEEvent): void {
    this.#events?.handle(event);
    if (event.event_type === "interaction.completed")
      this.#usage = toTokenUsage(event.interaction.usage);
  }

  async finish(): Promise<void> {
    const publisher = this.#publisher;
    if (publisher === null) return;
    try {
      publisher.publish({
        type: "model_interaction_ended",
        model: this.#model,
        usage: this.#usage,
      });
    } finally {
      await publisher[Symbol.asyncDispose]();
    }
  }
}

/**
 * Gemini's usage as the harness's provider-agnostic TokenUsage: the cross-provider totals only.
 * The per-modality breakdowns and grounding-tool counts stay on the interaction.completed event.
 */
export function toTokenUsage(usage: Interactions.Usage | undefined): TokenUsage | null {
  if (usage === undefined) return null;
  return {
    input_tokens: usage.total_input_tokens ?? null,
    output_tokens: usage.total_output_tokens ?? null,
    thought_tokens: usage.total_thought_tokens ?? null,
    cached_tokens: usage.total_cached_tokens ?? null,
    tool_use_tokens: usage.total_tool_use_tokens ?? null,
    total_tokens: usage.total_tokens ?? null,
  };
}

/**
 * Translates one interaction stream's events into turn events. Stateful across the stream, since a
 * tool call spans several events:
 *
 * - tool_requested, once per custom function call, when its step stops, with the arguments
 *   streamed over its arguments_delta fragments consolidated. The workflow's `runTool` owns the
 *   call's execution lifecycle (tool_start, tool_end, tool_error), so none is published here.
 * - tool_start, once per built-in (server-side) tool call, when its call step stops. A built-in
 *   tool runs inside Gemini with no request/dispatch gap, so the call is the execution and there is
 *   no tool_requested.
 * - tool_end, once per built-in tool, when its result step stops, as one payload.
 *
 * step.stop carries only its index, so each call and result step is recorded by index when it
 * starts, to match the stop back to what it closes.
 */
export class StreamEventPublisher {
  readonly #publish: (event: AgentStreamItem) => void;
  // Built-in tool call steps by index, published as tool_start when the step stops.
  readonly #pendingToolStarts = new Map<number, { toolId: string; toolName: string }>();
  // Built-in tool result steps by index, published as tool_end when the step stops. The tool id
  // is the result's call_id, so a call's tool_start and tool_end share it.
  readonly #pendingToolEnds = new Map<number, { toolId: string; toolName: string }>();
  // Function call steps by index, published as tool_requested when the step stops.
  readonly #pendingToolRequests = new Map<number, Interactions.FunctionCallStep>();
  // The JSON fragments of each function call's arguments, by step index.
  readonly #argBuffers = new Map<number, string>();

  constructor(publish: (event: AgentStreamItem) => void) {
    this.#publish = publish;
  }

  handle(event: InteractionSSEEvent): void {
    switch (event.event_type) {
      case "step.start":
        return this.#onStepStart(event);
      case "step.delta":
        return this.#onStepDelta(event);
      case "step.stop":
        return this.#onStepStop(event);
      // interaction.created, interaction.completed, interaction.status_update and error are not
      // step lifecycle events; the workflow reads the completed interaction and the error itself.
    }
  }

  #onStepStart({ index, step }: Interactions.StepStart): void {
    switch (step.type) {
      case "file_search_call":
        // file_search takes no arguments, so its tool_start has an empty input.
        this.#pendingToolStarts.set(index, { toolId: step.id, toolName: "file_search" });
        break;
      case "file_search_result":
        this.#pendingToolEnds.set(index, { toolId: step.call_id, toolName: "file_search" });
        break;
      case "function_call":
        // Its arguments may still stream as arguments_delta fragments.
        this.#pendingToolRequests.set(index, step);
        break;
      // Function results come back through runTool, and content steps (model_output, thought)
      // carry their content on their deltas.
    }
  }

  #onStepDelta({ index, delta }: Interactions.StepDelta): void {
    switch (delta.type) {
      case "text":
        if (delta.text) this.#publish({ type: "reply_delta", text: delta.text });
        break;
      case "thought_summary":
        this.#publish({ type: "thought_summary", delta: { ...delta } });
        break;
      case "text_annotation_delta":
        // A FileCitation's start_index and end_index are UTF-8 byte offsets into the assembled
        // reply text, not string indices.
        this.#publish({ type: "text_annotation", delta: { ...delta } });
        break;
      case "arguments_delta":
        if (delta.arguments)
          this.#argBuffers.set(index, (this.#argBuffers.get(index) ?? "") + delta.arguments);
        break;
      // file_search's call and result deltas are empty markers; other deltas belong to tools and
      // modalities this translation does not cover.
    }
  }

  #onStepStop({ index }: Interactions.StepStop): void {
    const start = this.#pendingToolStarts.get(index);
    if (start !== undefined) {
      this.#pendingToolStarts.delete(index);
      this.#publish({
        type: "tool_start",
        tool_id: start.toolId,
        tool_name: start.toolName,
        tool_input: {},
      });
      return;
    }
    const end = this.#pendingToolEnds.get(index);
    if (end !== undefined) {
      this.#pendingToolEnds.delete(index);
      this.#publish({
        type: "tool_end",
        tool_id: end.toolId,
        tool_name: end.toolName,
        tool_output: "",
      });
      return;
    }
    const call = this.#pendingToolRequests.get(index);
    if (call !== undefined) {
      this.#pendingToolRequests.delete(index);
      this.#publish({
        type: "tool_requested",
        tool_id: call.id,
        tool_name: call.name,
        tool_input: this.#consolidatedArgs(index, call),
      });
    }
  }

  /**
   * A function call's arguments: the streamed fragments when there are any, else the arguments on
   * the call step itself. Best effort, for display and approval: a malformed buffer yields {}
   * rather than failing the activity (the workflow parses its own copy of the stream).
   */
  #consolidatedArgs(index: number, call: Interactions.FunctionCallStep): Record<string, unknown> {
    const raw = this.#argBuffers.get(index) ?? "";
    this.#argBuffers.delete(index);
    if (!raw) return { ...call.arguments };
    try {
      const parsed: unknown = JSON.parse(raw);
      return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
        ? (parsed as Record<string, unknown>)
        : {};
    } catch {
      return {};
    }
  }
}
