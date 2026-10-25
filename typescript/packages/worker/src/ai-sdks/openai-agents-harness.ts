// Harness glue for the OpenAI Agents SDK, worker side (Python's openai_agents_harness.py, the half
// that runs in the streamed model activity): `OpenAIStreamObserver` folds one model call's
// agents-core stream events into the harness turn vocabulary and publishes them to the turn live,
// and `harnessObserverFactory` builds one per call from the routing token. The workflow half
// (`asOpenAIAgentTool(s)`, `runStreamed`, the MCP wrappers) is
// `@temporalio/agent-harness/ai-sdks/openai-agents-harness`.
//
// agents-core normalizes reply text into `output_text_delta` and closes each call with
// `response_done` (output items and usage) for every provider, and passes the provider's raw
// events through as `model` events. Reasoning summaries, annotations and the streamed function-call
// arguments exist only as raw OpenAI Responses events. A custom function call's name and call_id
// arrive on `response.output_item.added`, its JSON arguments stream over
// `response.function_call_arguments.delta` and finalize on `...done`, where they become ONE
// `tool_requested` keyed by the SDK call_id: the id `asOpenAIAgentTool` hands to `runTool`, so
// `tool_requested` and the call's `tool_start` / `tool_end` share a `tool_id`. `runTool` owns
// `tool_start` / `tool_end` / `tool_error`; hosted tools (web_search, file_search, ...) emit none.

import type { StreamEvent } from "@openai/agents-core";
import type { Duration } from "@temporalio/common";
import type {
  AgentStreamItem,
  TokenUsage,
  TurnStreamContext,
} from "@temporalio/agent-harness/harness/agent-protocol";
import { publisherFromActivity, type TurnEventPublisher } from "../harness/agent-workflow.ts";
import type { ObserverFactory, StreamObserver } from "./integration-helpers/stream-observer.ts";

/**
 * Where one streamed model call's events go, and the model it asked for (Python's
 * `_HarnessStreamToken`): the turn's stream context from `runStreamed`, and the activity input's
 * model name, both known at dispatch.
 */
export interface HarnessStreamToken {
  context: TurnStreamContext;
  model: string | null;
}

/** Opens the turn publisher an observer publishes through: `publisherFromActivity` by default. */
export type OpenTurnPublisher = (
  context: TurnStreamContext,
  options: { batchInterval?: Duration },
) => TurnEventPublisher;

export interface OpenAIStreamObserverOptions {
  /** The requested model id, named on both brackets. */
  model?: string | null;
  /** The publisher's flush cadence; the harness default when unset. */
  batchInterval?: Duration;
  /** How the turn's publisher is opened. Tests replace it with an in-memory sink. */
  openPublisher?: OpenTurnPublisher;
}

// The raw OpenAI Responses events read here, as agents-core passes them through.
interface RawFunctionCallItem {
  type: "function_call";
  id?: string;
  call_id: string;
  name: string;
}
type RawResponsesEvent =
  | { type: "response.output_item.added"; item?: { type?: string } }
  | { type: "response.function_call_arguments.delta"; item_id: string; delta?: string }
  | {
      type: "response.function_call_arguments.done";
      item_id: string;
      name?: string;
      arguments?: string;
    }
  | { type: "response.reasoning_summary_text.delta" }
  | { type: "response.output_text.annotation.added" };

/**
 * Translates one streamed OpenAI Agents model call into harness turn events, live. One instance
 * per call, so its per-call state (function-call argument buffers, captured usage) never bleeds
 * across the concurrent calls a worker runs. `start` opens the turn publisher and publishes
 * `model_interaction_started` at dispatch, before the first event, so the started -> ended span
 * measures the whole call (time to first token included); `finish` closes it with
 * `model_interaction_ended` carrying the call's usage, whether or not the stream failed.
 */
export class OpenAIStreamObserver implements StreamObserver<StreamEvent> {
  readonly #context: TurnStreamContext;
  readonly #model: string | null;
  readonly #batchInterval: Duration | undefined;
  readonly #openPublisher: OpenTurnPublisher;
  #publisher: TurnEventPublisher | null = null;
  // Function-call item id -> its call_id and tool name, from response.output_item.added.
  readonly #fnCalls = new Map<string, { callId: string; name: string }>();
  // Function-call item id -> the JSON argument fragments streamed so far.
  readonly #argBuffers = new Map<string, string>();
  // Call ids already published as tool_requested.
  readonly #requested = new Set<string>();
  #usage: TokenUsage | null = null;

  constructor(
    context: TurnStreamContext,
    {
      model = null,
      batchInterval,
      openPublisher = publisherFromActivity,
    }: OpenAIStreamObserverOptions = {},
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

  onEvent(event: StreamEvent): void {
    if (this.#publisher === null) return;
    switch (event.type) {
      case "output_text_delta":
        if (event.delta) this.#publish({ type: "reply_delta", text: event.delta });
        return;
      case "model":
        this.#onRawEvent(event.event as RawResponsesEvent);
        return;
      case "response_done": {
        const { usage, output } = event.response;
        this.#usage = toTokenUsage(usage);
        // A provider that streams no Responses function-call events still reports its calls here.
        for (const item of output) {
          if (item.type === "function_call" && !this.#requested.has(item.callId))
            this.#publishToolRequested(item.callId, item.name, item.arguments);
        }
        return;
      }
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

  #onRawEvent(raw: RawResponsesEvent | null | undefined): void {
    switch (raw?.type) {
      case "response.reasoning_summary_text.delta":
        // Reasoning summary text, dumped whole so a consumer can position it.
        this.#publish({ type: "thought_summary", delta: withoutNulls(raw) });
        return;
      case "response.output_text.annotation.added":
        this.#publish({ type: "text_annotation", delta: withoutNulls(raw) });
        return;
      case "response.output_item.added": {
        if (raw.item?.type !== "function_call") return;
        // A custom function call is opening; its arguments stream next and finalize on ...done.
        const item = raw.item as RawFunctionCallItem;
        const itemId = item.id || item.call_id;
        this.#fnCalls.set(itemId, { callId: item.call_id, name: item.name });
        if (!this.#argBuffers.has(itemId)) this.#argBuffers.set(itemId, "");
        return;
      }
      case "response.function_call_arguments.delta":
        if (raw.delta)
          this.#argBuffers.set(raw.item_id, (this.#argBuffers.get(raw.item_id) ?? "") + raw.delta);
        return;
      case "response.function_call_arguments.done": {
        // Prefers the done event's own arguments, falling back to the buffered fragments. If the
        // opening output_item.added was missed, degrades to the item id and the done event's name.
        const buffered = this.#argBuffers.get(raw.item_id) ?? "";
        this.#argBuffers.delete(raw.item_id);
        const call = this.#fnCalls.get(raw.item_id) ?? {
          callId: raw.item_id,
          name: raw.name ?? "",
        };
        this.#fnCalls.delete(raw.item_id);
        this.#publishToolRequested(call.callId, call.name, raw.arguments || buffered);
        return;
      }
      default:
        return;
    }
  }

  #publishToolRequested(callId: string, name: string, rawArguments: string): void {
    this.#requested.add(callId);
    this.#publish({
      type: "tool_requested",
      tool_id: callId,
      tool_name: name,
      tool_input: parseToolArgs(rawArguments),
    });
  }

  #publish(event: AgentStreamItem): void {
    this.#publisher?.publish(event);
  }
}

/**
 * Builds a fresh `OpenAIStreamObserver` for one streamed call from its routing token. The default
 * observer factory of the harness's `OpenAIAgentsPlugin`; `batchInterval` is the run's
 * `streamingBatchInterval`.
 */
export const harnessObserverFactory: ObserverFactory<HarnessStreamToken, StreamEvent> = (
  token,
  { batchInterval },
) =>
  new OpenAIStreamObserver(token.context, {
    model: token.model,
    ...(batchInterval === undefined ? {} : { batchInterval }),
  });

/**
 * Best-effort parse of a function call's JSON arguments, for display and approval only: a
 * malformed or non-object payload yields `{}` rather than failing the model activity.
 */
function parseToolArgs(raw: string): Record<string, unknown> {
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

type UsageDetails = Record<string, number> | Record<string, number>[] | undefined;

/** One token-details count, summed when agents-core reports per-request details. */
function detail(details: UsageDetails, key: string): number | null {
  const records = details === undefined ? [] : Array.isArray(details) ? details : [details];
  const counts = records.flatMap((r) => (typeof r[key] === "number" ? [r[key]] : []));
  return counts.length ? counts.reduce((a, b) => a + b, 0) : null;
}

/** agents-core's usage as the harness's TokenUsage; OpenAI reports no tool-use count. */
function toTokenUsage(usage: {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  inputTokensDetails?: UsageDetails;
  outputTokensDetails?: UsageDetails;
}): TokenUsage {
  return {
    input_tokens: usage.inputTokens,
    output_tokens: usage.outputTokens,
    total_tokens: usage.totalTokens,
    thought_tokens: detail(usage.outputTokensDetails, "reasoning_tokens"),
    cached_tokens: detail(usage.inputTokensDetails, "cached_tokens"),
    tool_use_tokens: null,
  };
}

/** A raw event as Python's `model_dump(exclude_none=True)` gives it: no nulls at any depth. */
function withoutNulls(value: object): Record<string, unknown> {
  const strip = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(strip);
    if (v !== null && typeof v === "object")
      return Object.fromEntries(
        Object.entries(v).flatMap(([k, x]) =>
          x === null || x === undefined ? [] : [[k, strip(x)]],
        ),
      );
    return v;
  };
  return strip(value) as Record<string, unknown>;
}
