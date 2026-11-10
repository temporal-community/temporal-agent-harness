// The wire types of the Strands model activities, which workflow code sends. The activities
// themselves (`ModelActivity`) run on the worker:
// `@temporalio/agent-harness-worker/ai-sdks/strands-agents`.
import type { JSONValue, Message, ModelStreamEvent, StreamOptions } from '@strands-agents/sdk';
import type { Duration } from '@temporalio/common';

/**
 * Input for the `invokeModel` activity. Mirrors `Model.stream(messages, options)`
 * with the chosen factory name carried alongside.
 */
export interface InvokeModelInput {
  modelName?: string;
  messages: Message[];
  options?: StreamOptions;
}

/**
 * Input for the `invokeModelStreaming` activity. Adds the workflow-stream topic
 * to which each event is published, and the batch interval for publishes.
 */
export interface InvokeModelStreamingInput extends InvokeModelInput {
  streamingTopic: string;
  streamingBatchInterval: Duration;
  // Harness: an opaque routing token for the plugin's observer factory. When set, the call's
  // events go to the observer the factory builds from it instead of `streamingTopic`.
  streamTo?: unknown;
}

/**
 * Input for the stateful model activities: a copy of the agent's model state, such as the last
 * response id, which the model reads and updates.
 */
export interface InvokeStatefulModelInput extends InvokeModelInput {
  modelState: Record<string, JSONValue>;
}

/** Input for the streaming stateful model activity. */
export interface InvokeStatefulModelStreamingInput extends InvokeModelStreamingInput {
  modelState: Record<string, JSONValue>;
}

/** A stateful model's events and its updated model state. */
export interface InvokeStatefulModelResult {
  events: ModelStreamEvent[];
  modelState: Record<string, JSONValue>;
}

/**
 * Input for the model config activity: the factory name, as in {@link InvokeModelInput}.
 */
export interface GetModelConfigInput {
  modelName?: string;
}

/**
 * The parts of the worker model that Strands reads in workflow code: the model id for traces, the
 * context window for proactive compression, and whether the model keeps the conversation
 * server-side. Nothing else is returned, so provider settings such as credentials never reach the
 * workflow history.
 */
export interface ModelConfigInfo {
  modelId?: string;
  contextWindowLimit?: number;
  stateful?: boolean;
}
