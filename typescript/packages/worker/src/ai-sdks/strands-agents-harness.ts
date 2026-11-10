// Harness glue for the Strands Agents SDK, worker side (Python's strands_agents_harness.py, the half
// that runs in the model activity): `StrandsStreamObserver` folds one model call's Strands stream
// events into the harness turn vocabulary and publishes them to the turn live, and
// `harnessObserverFactory` builds one per call from the routing token. Wire it onto the plugin as
// `new StrandsPlugin({ models, observerFactory: harnessObserverFactory })`. The workflow half
// (`harnessStrandsAgent`, `asStrandsTool(s)`, `harnessStreamRoute`) is
// `@temporalio/agent-harness/ai-sdks/strands-agents-harness`.
//
// A tool call streams as a `toolUseStart` block start (its toolUseId and name), input fragments
// in `toolUseInputDelta`s, and the block's stop, where it becomes ONE `tool_requested` keyed by
// the toolUseId: the id `asStrandsTool` hands to `runTool`, so `tool_requested` and the call's
// `tool_start` / `tool_end` share a `tool_id`.

import type { ModelStreamEvent } from "@strands-agents/sdk";
import type { Duration } from "@temporalio/common";
import { ApplicationFailure } from "@temporalio/common";
import { z } from "zod";
import {
  TurnStreamContextSchema,
  type AgentStreamItem,
  type TokenUsage,
  type TurnStreamContext,
} from "@temporalio/agent-harness/harness/agent-protocol";
import type { HarnessStreamToken } from "@temporalio/agent-harness/ai-sdks/strands-agents-harness";
import { publisherFromActivity, type TurnEventPublisher } from "../harness/agent-workflow.ts";
import type { ObserverFactory, StreamObserver } from "./integration-helpers/stream-observer.ts";

/** Opens the turn publisher an observer publishes through: `publisherFromActivity` by default. */
export type OpenTurnPublisher = (
  context: TurnStreamContext,
  options: { batchInterval?: Duration },
) => TurnEventPublisher;

export interface StrandsStreamObserverOptions {
  /** The model id, named on both brackets. */
  model?: string | null;
  /** The publisher's flush cadence; the harness default when unset. */
  batchInterval?: Duration;
  /** How the turn's publisher is opened. Tests replace it with an in-memory sink. */
  openPublisher?: OpenTurnPublisher;
}

/**
 * Translates one streamed Strands model call into harness turn events, live. One instance per
 * call, so its per-call state (the open tool-use block, captured usage) never mixes across
 * concurrent calls on a worker. `start` opens the turn publisher and publishes
 * `model_interaction_started` at dispatch, before the first event; `finish` always publishes
 * `model_interaction_ended` with the usage from the stream's metadata event.
 */
export class StrandsStreamObserver implements StreamObserver<ModelStreamEvent> {
  readonly #context: TurnStreamContext;
  readonly #model: string | null;
  readonly #batchInterval: Duration | undefined;
  readonly #openPublisher: OpenTurnPublisher;
  #publisher: TurnEventPublisher | null = null;
  // The open tool-use block: its toolUseId, name and the input fragments streamed so far.
  #toolUse: { id: string; name: string; input: string } | null = null;
  #usage: TokenUsage | null = null;

  constructor(
    context: TurnStreamContext,
    {
      model = null,
      batchInterval,
      openPublisher = publisherFromActivity,
    }: StrandsStreamObserverOptions = {},
  ) {
    this.#context = context;
    this.#model = model;
    this.#batchInterval = batchInterval;
    this.#openPublisher = openPublisher;
  }

  async start(): Promise<void> {
    this.#publisher = this.#openPublisher(
      this.#context,
      this.#batchInterval === undefined ? {} : { batchInterval: this.#batchInterval },
    );
    this.#publish({ type: "model_interaction_started", model: this.#model });
  }

  onEvent(event: ModelStreamEvent): void {
    if (this.#publisher === null) return;
    switch (event.type) {
      case "modelContentBlockStartEvent":
        if (event.start?.type === "toolUseStart")
          this.#toolUse = { id: event.start.toolUseId, name: event.start.name, input: "" };
        return;
      case "modelContentBlockDeltaEvent": {
        const { delta } = event;
        if (delta.type === "textDelta") {
          if (delta.text) this.#publish({ type: "reply_delta", text: delta.text });
        } else if (delta.type === "reasoningContentDelta") {
          const thought: Record<string, unknown> = {};
          if (delta.text) thought.text = delta.text;
          if (delta.signature) thought.signature = delta.signature;
          if (Object.keys(thought).length > 0)
            this.#publish({ type: "thought_summary", delta: thought });
        } else if (delta.type === "toolUseInputDelta" && this.#toolUse !== null) {
          this.#toolUse.input += delta.input;
        }
        return;
      }
      case "modelContentBlockStopEvent": {
        const toolUse = this.#toolUse;
        if (toolUse === null) return;
        this.#toolUse = null;
        this.#publish({
          type: "tool_requested",
          tool_id: toolUse.id,
          tool_name: toolUse.name,
          tool_input: parseToolInput(toolUse.input),
        });
        return;
      }
      case "modelMetadataEvent":
        if (event.usage)
          this.#usage = {
            input_tokens: event.usage.inputTokens,
            output_tokens: event.usage.outputTokens,
            total_tokens: event.usage.totalTokens,
            thought_tokens: null,
            cached_tokens: event.usage.cacheReadInputTokens ?? null,
            tool_use_tokens: null,
          };
        return;
      default:
        return;
    }
  }

  async finish(error?: unknown): Promise<void> {
    const publisher = this.#publisher;
    if (publisher === null) return;
    try {
      // Always close the started bracket, even when the stream failed (usage then stays null).
      this.#publish({ type: "model_interaction_ended", model: this.#model, usage: this.#usage });
    } finally {
      this.#publisher = null;
      try {
        await publisher[Symbol.asyncDispose]();
      } catch (drainError) {
        // The model's own failure wins over a secondary flush failure.
        if (error === undefined) throw drainError;
      }
    }
  }

  #publish(event: AgentStreamItem): void {
    this.#publisher?.publish(event);
  }
}

const HarnessStreamTokenSchema = z.object({
  context: TurnStreamContextSchema,
  model: z.string().nullable(),
});

/**
 * Builds a fresh `StrandsStreamObserver` for one streamed call from its routing token, the
 * `HarnessStreamToken` that `harnessStreamRoute` made, which arrives as plain JSON. Wire it onto
 * the plugin as `new StrandsPlugin({ observerFactory: harnessObserverFactory })`.
 */
export const harnessObserverFactory: ObserverFactory<unknown, ModelStreamEvent> = (
  token,
  { batchInterval },
) => {
  const parsed = HarnessStreamTokenSchema.safeParse(token);
  if (!parsed.success)
    throw ApplicationFailure.create({
      type: "HarnessStreamRouteMissing",
      nonRetryable: true,
      message:
        "The streamed model call's routing token is not a harness stream route. Build the agent " +
        "with harnessStrandsAgent() from @temporalio/agent-harness/ai-sdks/strands-agents-harness.",
    });
  const route: HarnessStreamToken = parsed.data;
  return new StrandsStreamObserver(route.context, {
    model: route.model,
    ...(batchInterval === undefined ? {} : { batchInterval }),
  });
};

/**
 * Best-effort parse of a tool call's streamed JSON input, for display and approval only: a
 * malformed or non-object payload yields `{}` rather than failing the model activity.
 */
function parseToolInput(raw: string): Record<string, unknown> {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}
