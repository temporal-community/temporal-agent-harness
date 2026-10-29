// The streamed model activity the harness registers in place of `@temporalio/openai-agents`'
// `invokeModelStreamActivity` (Python's openai_agents/_invoke_model_activity.py, streaming half).
// It runs the model call as the official one does (same name, input and return value, so the
// official workflow-side model stub drives it unchanged) but hands each stream event to a per-call
// observer that publishes harness turn events, instead of publishing raw events to a topic.
// Adapted from @temporalio/openai-agents 1.24.0 src/worker/activities.ts and heartbeat.ts (MIT).

import type {
  AgentInputItem,
  ModelProvider,
  ModelRequest,
  StreamEvent,
} from "@openai/agents-core";
import { APIError } from "openai";
import { activityInfo, heartbeat } from "@temporalio/activity";
import { ApplicationFailure, type Duration } from "@temporalio/common";
import { z } from "zod";
import { TurnStreamContextSchema } from "@temporalio/agent-harness/harness/agent-protocol";
import {
  observeStream,
  type ObserverFactory,
} from "../integration-helpers/stream-observer.ts";
import type { HarnessStreamToken } from "../openai-agents-harness.ts";

/** The activity name the official workflow-side model stub schedules for a streamed call. */
export const INVOKE_MODEL_STREAM_ACTIVITY = "invokeModelStreamActivity";

/** Python's `ModelActivityParameters.streaming_batch_interval` default. */
export const DEFAULT_STREAMING_BATCH_INTERVAL: Duration = "100 milliseconds";

/** The routing token `runStreamed` serializes into the run's `streamingTopic`. */
const HarnessStreamRouteSchema = z.object({ context: TurnStreamContextSchema });

// The wire shapes of `@temporalio/openai-agents`' common/serialized-model.ts, which it does not
// export.
type JsonValue = null | string | number | boolean | JsonValue[] | { [k: string]: JsonValue };

interface SerializedModelRequest {
  systemInstructions?: string;
  input: JsonValue;
  modelSettings: ModelRequest["modelSettings"];
  tools: ModelRequest["tools"];
  toolsExplicitlyProvided?: boolean;
  outputType: ModelRequest["outputType"];
  handoffs: ModelRequest["handoffs"];
  prompt?: JsonValue;
  previousResponseId?: string;
  conversationId?: string;
  tracing: JsonValue;
  overridePromptModel?: boolean;
}

export interface InvokeModelStreamActivityInput {
  modelName: string;
  request: SerializedModelRequest;
  /** The serialized harness route; see `runStreamed`. */
  streamingTopic: string;
  /** Stream publisher flush interval, as a Duration string. */
  streamingBatchInterval?: string;
}

/** Each StreamEvent as JSON, which it is by construction. */
export type SerializedStreamEvent = JsonValue;

function fromSerializedModelRequest(wire: SerializedModelRequest): ModelRequest {
  return {
    systemInstructions: wire.systemInstructions,
    input: wire.input as string | AgentInputItem[],
    modelSettings: wire.modelSettings,
    tools: wire.tools,
    toolsExplicitlyProvided: wire.toolsExplicitlyProvided,
    outputType: wire.outputType,
    handoffs: wire.handoffs,
    prompt: wire.prompt as ModelRequest["prompt"],
    previousResponseId: wire.previousResponseId,
    conversationId: wire.conversationId,
    tracing: wire.tracing as ModelRequest["tracing"],
    overridePromptModel: wire.overridePromptModel,
  };
}

/** The harness route out of `streamingTopic`, or a non-retryable failure naming the mistake. */
function routeOf(streamingTopic: string): HarnessStreamToken["context"] {
  let json: unknown;
  try {
    json = JSON.parse(streamingTopic);
  } catch {
    json = undefined;
  }
  const route = HarnessStreamRouteSchema.safeParse(json);
  if (route.success) return route.data.context;
  throw ApplicationFailure.create({
    type: "HarnessStreamRouteMissing",
    nonRetryable: true,
    message:
      `The streamed model call's streamingTopic (${JSON.stringify(streamingTopic)}) is not a ` +
      "harness stream route. Run the agent with runStreamed() from " +
      "@temporalio/agent-harness/ai-sdks/openai-agents-harness, and leave the plugin's " +
      "modelParams.streamingTopic unset.",
  });
}

/**
 * Classify a model failure for Temporal's retry policy: an OpenAI APIError by its status (and the
 * `x-should-retry` / `retry-after` headers), anything else as retryable.
 */
export function toModelInvocationFailure(error: unknown): ApplicationFailure {
  if (error instanceof ApplicationFailure) return error;
  if (error instanceof APIError) {
    const { status, headers } = error;
    // Prefer retry-after-ms (ms precision) over Retry-After (seconds).
    let nextRetryDelay: number | undefined;
    const ms = parseFloat(headers?.get("retry-after-ms") ?? "");
    if (!Number.isNaN(ms)) nextRetryDelay = ms;
    else {
      const s = parseFloat(headers?.get("retry-after") ?? "");
      if (!Number.isNaN(s)) nextRetryDelay = s * 1000;
    }
    const shouldRetry = headers?.get("x-should-retry");
    const nonRetryable =
      shouldRetry === "true"
        ? false
        : shouldRetry === "false"
          ? true
          : !(
              status !== undefined &&
              (status === 408 || status === 409 || status === 429 || status >= 500)
            );
    let type = "ModelInvocationError";
    if (status === 429) type = "ModelInvocationError.RateLimit";
    else if (status === 401 || status === 403) type = "ModelInvocationError.Authentication";
    else if (status === 400 || status === 422) type = "ModelInvocationError.BadRequest";
    else if (status === 408) type = "ModelInvocationError.Timeout";
    else if (status === 409) type = "ModelInvocationError.Conflict";
    else if (status !== undefined && status >= 500) type = "ModelInvocationError.ServerError";
    return ApplicationFailure.create({
      message: `Model invocation failed: ${error.message}`,
      type,
      nonRetryable,
      cause: error,
      ...(nextRetryDelay !== undefined ? { nextRetryDelay } : {}),
    });
  }
  const message = error instanceof Error ? error.message : String(error);
  return ApplicationFailure.create({
    message: `Model invocation failed: ${message}`,
    type: "ModelInvocationError",
    nonRetryable: false,
    cause: error instanceof Error ? error : new Error(message),
  });
}

/**
 * Heartbeat at half the activity's heartbeat timeout, starting now, until the returned function is
 * called, so a long wait for the first token does not trip the timeout. A no-op when the activity
 * has none.
 */
function startAdaptiveHeartbeat(): () => void {
  const timeout = activityInfo().heartbeatTimeoutMs;
  if (!timeout || timeout <= 0) return () => {};
  const beat = () => {
    try {
      heartbeat();
    } catch {
      // A cancelled activity's heartbeat throws; the model call's own outcome is what matters.
    }
  };
  beat();
  const timer = setInterval(beat, timeout / 2);
  return () => clearInterval(timer);
}

/**
 * The harness's `invokeModelStreamActivity`: resolves the model, streams the call through a fresh
 * observer from `observerFactory` (built from the run's harness route and the requested model),
 * and returns every event for the workflow-side stub to replay into the agent loop.
 */
export function createInvokeModelStreamActivity(
  modelProvider: ModelProvider,
  observerFactory: ObserverFactory<HarnessStreamToken, StreamEvent>,
): (input: InvokeModelStreamActivityInput) => Promise<SerializedStreamEvent[]> {
  return async function invokeModelStreamActivity(input) {
    const context = routeOf(input.streamingTopic);
    const stopHeartbeat = startAdaptiveHeartbeat();
    try {
      const request = fromSerializedModelRequest(input.request);
      const events: SerializedStreamEvent[] = [];
      try {
        const model = await modelProvider.getModel(input.modelName);
        const observer = observerFactory(
          { context, model: input.modelName },
          { batchInterval: input.streamingBatchInterval ?? DEFAULT_STREAMING_BATCH_INTERVAL },
        );
        await observeStream(observer, model.getStreamedResponse(request), (event) =>
          events.push(event as unknown as SerializedStreamEvent),
        );
      } catch (error) {
        throw toModelInvocationFailure(error);
      }
      return events;
    } finally {
      stopHeartbeat();
    }
  };
}
