import { ContextWindowOverflowError, Model } from '@strands-agents/sdk';
import type { BaseModelConfig, Message, ModelStreamEvent, StreamOptions } from '@strands-agents/sdk';
import * as workflow from '@temporalio/workflow';
import type { ActivityOptions } from '@temporalio/workflow';
import type { Duration } from '@temporalio/common/lib/time.js';
import { applicationFailureType, CONTEXT_WINDOW_OVERFLOW_TYPE } from './strands-errors.ts';
import type {
  GetModelConfigInput,
  InvokeModelInput,
  InvokeModelStreamingInput,
  InvokeStatefulModelInput,
  InvokeStatefulModelResult,
  InvokeStatefulModelStreamingInput,
  ModelConfigInfo,
} from './model-activity.ts';

/** Name of the activity that returns the worker model's {@link ModelConfigInfo}. */
export const GET_MODEL_CONFIG_ACTIVITY = 'strandsGetModelConfig';

/** Names of the activities that run a stateful model with the agent's model state. */
export const INVOKE_STATEFUL_MODEL_ACTIVITY = 'strandsInvokeStatefulModel';
export const INVOKE_STATEFUL_MODEL_STREAMING_ACTIVITY = 'strandsInvokeStatefulModelStreaming';

/**
 * Options for {@link TemporalModel}.
 *
 * The {@link ActivityOptions} are applied to every activity invocation
 * `stream()` schedules. `streamingTopic` and `streamingBatchInterval`
 * forward {@link ModelStreamEvent}s on a {@link WorkflowStream} as
 * they are produced inside the model activity.
 */
export interface TemporalModelOptions {
  modelName?: string;
  activityOptions?: ActivityOptions;
  streamingTopic?: string;
  streamingBatchInterval?: Duration;
  /**
   * Harness: called in workflow code before each model call with the model's id (or its registered
   * name when the model reports no id). A result other than `undefined` is a routing token for the
   * plugin's observer factory: the call runs as a streaming activity and its events go to that
   * observer instead of `streamingTopic`.
   */
  streamRoute?: (model: string | undefined) => unknown;
}

/**
 * A Strands {@link Model} whose {@link Model.stream} runs as a Temporal
 * activity. Constructing a `TemporalModel` does no I/O, so instances are
 * safe to create at module load time.
 *
 * The `modelName` selects which factory the plugin will invoke worker-side;
 * it must match a key in {@link StrandsPlugin}'s `models` map. A name not
 * present in `models` raises an error inside the model activity at first call.
 *
 * `TemporalAgent` reads the worker model's config before its first call. With a plain Strands
 * `Agent`, call `await model.loadConfig()` before building the agent: without it, the model reports
 * itself as stateless, and a stateful worker model fails every call.
 */
export class TemporalModel extends Model<BaseModelConfig> {
  private readonly modelName?: string;
  private readonly activityOptions: ActivityOptions;
  private readonly streamingTopic?: string;
  private readonly streamingBatchInterval: Duration;
  // Harness: per-call routing token for the plugin's observer factory.
  private readonly streamRoute: ((model: string | undefined) => unknown) | undefined;
  private config: ModelConfigInfo = {};
  private isStateful = false;
  private configLoaded = false;

  constructor(options: TemporalModelOptions = {}) {
    super();
    this.modelName = options.modelName;
    this.activityOptions = options.activityOptions ?? {};
    this.streamingTopic = options.streamingTopic;
    this.streamingBatchInterval = options.streamingBatchInterval ?? '100 milliseconds';
    this.streamRoute = options.streamRoute;
  }

  override updateConfig(_config: BaseModelConfig): void {
    // No-op; the real model is configured worker-side via the plugin's factories.
  }

  /**
   * The worker model's id and context window, once {@link loadConfig} has read them; otherwise
   * empty.
   */
  override getConfig(): BaseModelConfig {
    return { ...this.config };
  }

  /** Whether the worker model keeps the conversation server-side, once {@link loadConfig} has read it. */
  override get stateful(): boolean {
    return this.isStateful;
  }

  /**
   * Read the worker model's id, context window and stateful flag through an activity, once per
   * instance, so that Strands' traces, proactive compression and stateful handling use the real
   * model's values. {@link TemporalAgent} calls this before Strands initializes the agent.
   *
   * Returns `undefined`, without awaiting anything, when the config was already read, so callers
   * can go on synchronously.
   */
  loadConfig(): Promise<void> | undefined {
    if (this.configLoaded) return undefined;
    const activities = workflow.proxyActivities<{
      [GET_MODEL_CONFIG_ACTIVITY](input: GetModelConfigInput): Promise<ModelConfigInfo>;
    }>({
      startToCloseTimeout: '10 minutes',
      ...this.activityOptions,
    });
    return activities[GET_MODEL_CONFIG_ACTIVITY]({ modelName: this.modelName }).then(({ stateful, ...config }) => {
      this.config = config;
      // A result recorded before the flag existed has none: the model ran as stateless then.
      this.isStateful = stateful === true;
      this.configLoaded = true;
    });
  }

  override async *stream(messages: Message[], options?: StreamOptions): AsyncIterable<ModelStreamEvent> {
    const proxiedActivities = workflow.proxyActivities<{
      invokeModel(input: InvokeModelInput): Promise<ModelStreamEvent[]>;
      invokeModelStreaming(input: InvokeModelStreamingInput): Promise<ModelStreamEvent[]>;
      [INVOKE_STATEFUL_MODEL_ACTIVITY](input: InvokeStatefulModelInput): Promise<InvokeStatefulModelResult>;
      [INVOKE_STATEFUL_MODEL_STREAMING_ACTIVITY](
        input: InvokeStatefulModelStreamingInput
      ): Promise<InvokeStatefulModelResult>;
    }>({
      startToCloseTimeout: '10 minutes',
      ...this.activityOptions,
    });

    const { modelState, ...rest } = options ?? {};
    const input = { modelName: this.modelName, messages, options: this.isStateful ? rest : options };
    // Harness: resolve the call's routing token in workflow code.
    const streamTo = this.streamRoute?.(this.config.modelId ?? this.modelName);
    const streaming =
      this.streamingTopic !== undefined || streamTo !== undefined
        ? {
            streamingTopic: this.streamingTopic ?? '',
            streamingBatchInterval: this.streamingBatchInterval,
            ...(streamTo === undefined ? {} : { streamTo }),
          }
        : undefined;

    if (!this.isStateful) {
      let events: ModelStreamEvent[];
      try {
        events = streaming
          ? await proxiedActivities.invokeModelStreaming({ ...input, ...streaming })
          : await proxiedActivities.invokeModel(input);
      } catch (err) {
        // Give the agent Strands' own error for a context overflow, so its conversation manager can
        // trim the conversation and call the model again. Keep the activity failure as its cause.
        if (
          err instanceof workflow.ActivityFailure &&
          applicationFailureType(err.cause) === CONTEXT_WINDOW_OVERFLOW_TYPE
        ) {
          throw Object.assign(new ContextWindowOverflowError(err.cause?.message ?? ''), { cause: err });
        }
        throw err;
      }
      yield* events;
      return;
    }

    // A stateful model's overflow is not rethrown as Strands' error: its conversation is on the
    // server, and the default manager the agent keeps would trim and retry where Strands, which
    // gives a stateful agent no manager, fails the turn.

    // The agent's model state is a live store; send a copy of its values, and copy the updated
    // values back, as the model would have written them natively.
    const statefulInput = { ...input, modelState: modelState?.getAll() ?? {} };
    const result = streaming
      ? await proxiedActivities[INVOKE_STATEFUL_MODEL_STREAMING_ACTIVITY]({ ...statefulInput, ...streaming })
      : await proxiedActivities[INVOKE_STATEFUL_MODEL_ACTIVITY](statefulInput);
    if (modelState) {
      modelState.clear();
      for (const [key, value] of Object.entries(result.modelState)) modelState.set(key, value);
    }
    yield* result.events;
  }
}
