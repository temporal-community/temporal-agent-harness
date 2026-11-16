import { Message, StateStore } from '@strands-agents/sdk';
import type { Model, ModelStreamEvent, StreamOptions } from '@strands-agents/sdk';
import { Context } from '@temporalio/activity';
import { ApplicationFailure } from '@temporalio/common';
import { WorkflowStreamClient } from '@temporalio/workflow-streams/client';
import type {
  GetModelConfigInput,
  InvokeModelInput,
  InvokeModelStreamingInput,
  InvokeStatefulModelInput,
  InvokeStatefulModelResult,
  InvokeStatefulModelStreamingInput,
  ModelConfigInfo,
} from '@temporalio/agent-harness/ai-sdks/strands-agents';
import { autoHeartbeat } from './heartbeat-decorator.ts';
// Harness: the live observer for calls that carry a routing token.
import { observeStream, type ObserverFactory } from '../integration-helpers/stream-observer.ts';

/**
 * Reconstruct Strands `Message` instances from the wire data the workflow
 * payload converter delivered.
 *
 * Strands content-block and `Message` classes override `toJSON()` to project
 * themselves into Bedrock-style wire data (e.g. `TextBlock` → `{ text }`,
 * `ToolUseBlock` → `{ toolUse: {...} }`). Temporal's default payload
 * converter serializes activity input via `JSON.stringify`, which calls
 * those `toJSON`s, dropping the `type` discriminator and reshaping every
 * block. The receiving side sees plain Bedrock-shape objects that don't
 * match what `BedrockModel._formatContentBlock` (or any other Strands
 * `Model.stream` implementation) expects.
 *
 * `Message.fromMessageData` knows how to rebuild a `Message` and its blocks
 * from that wire shape via `contentBlockFromData`, so call it once on every
 * incoming message before handing them to the user-supplied `Model`.
 */
function rebuildMessages(messages: unknown): Message[] {
  if (!Array.isArray(messages)) {
    return [];
  }
  return messages.map((m) =>
    m instanceof Message ? m : Message.fromMessageData(m as Parameters<typeof Message.fromMessageData>[0])
  );
}

/**
 * Run `model.stream` with the activity's own cancellation signal as `cancelSignal`. Strands passes
 * the agent's `cancelSignal`, an `AbortSignal`, which reaches the activity as a plain object.
 *
 * Once the activity's cancellation reaches the worker, the signal is aborted. That stops the model
 * call only if the model passes `cancelSignal` to its client (Strands 1.19.0's `AnthropicModel`
 * does not); otherwise the call runs on and the activity can complete with its result. A remote
 * activity learns of cancellation only through heartbeats, so this needs a `heartbeatTimeout` in
 * the activity options. Model clients abort with their own errors (OpenAI's is named `Error`),
 * which Temporal would record as a failure, so after cancellation the activity throws the signal's
 * `CancelledFailure` instead.
 */
async function* streamWithActivityCancellation(
  model: Model,
  messages: Message[],
  options?: StreamOptions
): AsyncGenerator<ModelStreamEvent> {
  const signal = Context.current().cancellationSignal;
  try {
    yield* model.stream(messages, { ...options, cancelSignal: signal });
  } catch (err) {
    if (signal.aborted) throw signal.reason;
    throw err;
  }
}

/**
 * Holds the model factory map and exposes the model activities.
 *
 * Models are constructed lazily on first use and cached for the worker's
 * lifetime. `defaultName` is only set by the plugin's own implicit
 * default-Bedrock fallback; user-supplied factories force `model: 'name'`
 * on every {@link TemporalAgent}.
 *
 * The model activities heartbeat at half the configured `heartbeatTimeout` via
 * {@link autoHeartbeat} so long model calls don't stall the worker's view
 * of activity liveness.
 */
export class ModelActivity {
  private readonly models: Map<string, Model> = new Map();
  // Harness: fields, not constructor parameter properties, under the repo's compiler settings.
  private readonly factories: Record<string, () => Model>;
  private readonly defaultName: string | undefined;
  // Harness: builds the live observer for calls that carry a `streamTo` token.
  private readonly observerFactory: ObserverFactory<unknown, ModelStreamEvent> | undefined;

  constructor(
    factories: Record<string, () => Model>,
    defaultName?: string,
    observerFactory?: ObserverFactory<unknown, ModelStreamEvent>
  ) {
    this.factories = factories;
    this.defaultName = defaultName;
    this.observerFactory = observerFactory;
    this.invokeModel = autoHeartbeat(this.invokeModel.bind(this));
    this.invokeModelStreaming = autoHeartbeat(this.invokeModelStreaming.bind(this));
    this.invokeStatefulModel = autoHeartbeat(this.invokeStatefulModel.bind(this));
    this.invokeStatefulModelStreaming = autoHeartbeat(this.invokeStatefulModelStreaming.bind(this));
  }

  private getModel(name?: string): Model {
    const resolved = name ?? this.defaultName;
    if (resolved === undefined) {
      throw new Error(
        `TemporalAgent was constructed without an explicit \`model\`, but the plugin was configured ` +
          `with user-supplied \`models\`. Pass model='...' to TemporalAgent. ` +
          `Known: ${Object.keys(this.factories).sort().join(', ')}`
      );
    }
    let model = this.models.get(resolved);
    if (model === undefined) {
      const factory = this.factories[resolved];
      if (factory === undefined) {
        throw new Error(`Unknown model name '${resolved}'. Known: ${Object.keys(this.factories).sort().join(', ')}`);
      }
      model = factory();
      this.models.set(resolved, model);
    }
    return model;
  }

  async getModelConfig(input: GetModelConfigInput): Promise<ModelConfigInfo> {
    const model = this.getModel(input.modelName);
    const { modelId, contextWindowLimit } = model.getConfig();
    return { modelId, contextWindowLimit, stateful: model.stateful };
  }

  async invokeModel(input: InvokeModelInput): Promise<ModelStreamEvent[]> {
    return this.runModel(this.getStatelessModel(input.modelName), input);
  }

  async invokeModelStreaming(input: InvokeModelStreamingInput): Promise<ModelStreamEvent[]> {
    return this.runModelStreaming(this.getStatelessModel(input.modelName), input);
  }

  /**
   * The model for a stateless call. A stateful model cannot run here without the agent's model
   * state, and retrying does not bring it: the workflow chose this activity because its config
   * read had no stateful flag, from a worker without stateful support or from a model that became
   * stateful later, or because `TemporalModel` runs without `TemporalAgent` and never read the
   * config.
   */
  private getStatelessModel(name?: string): Model {
    const model = this.getModel(name);
    if (model.stateful) {
      throw ApplicationFailure.create({
        type: 'StatefulModelOnStatelessPath',
        nonRetryable: true,
        message:
          `The worker model ${JSON.stringify(name ?? this.defaultName)} is stateful, but the workflow ` +
          `called it without its model state. Its config was read from a worker without stateful ` +
          `support, the worker model became stateful after the run read its config, or ` +
          `TemporalModel runs without TemporalAgent and did not read it. Reset or terminate the run.`,
      });
    }
    return model;
  }

  /**
   * The model for a stateful call. A model that is no longer stateful ignores the model state, and
   * the agent, which read a stateful config, clears its messages after each turn, so the model
   * would see only each turn's new message: the worker model became stateless after the run read
   * its config.
   */
  private getStatefulModel(name?: string): Model {
    const model = this.getModel(name);
    if (!model.stateful) {
      throw ApplicationFailure.create({
        type: 'StatelessModelOnStatefulPath',
        nonRetryable: true,
        message:
          `The worker model ${JSON.stringify(name ?? this.defaultName)} is not stateful, but the run ` +
          `read a stateful config, so its agent does not keep the messages this model needs. The ` +
          `worker model became stateless after the run read its config. Reset or terminate the run.`,
      });
    }
    return model;
  }

  private async runModel(model: Model, input: InvokeModelInput): Promise<ModelStreamEvent[]> {
    const messages = rebuildMessages(input.messages);
    const events: ModelStreamEvent[] = [];
    for await (const event of streamWithActivityCancellation(model, messages, input.options)) {
      events.push(event);
    }
    return events;
  }

  private async runModelStreaming(model: Model, input: InvokeModelStreamingInput): Promise<ModelStreamEvent[]> {
    const messages = rebuildMessages(input.messages);
    // Harness: a call carrying a routing token streams to the observer the factory builds from it,
    // and does not publish to `streamingTopic`.
    if (input.streamTo !== undefined) {
      const events: ModelStreamEvent[] = [];
      const stream = streamWithActivityCancellation(model, messages, input.options);
      if (this.observerFactory === undefined) {
        for await (const event of stream) events.push(event);
      } else {
        const observer = this.observerFactory(input.streamTo, {
          batchInterval: input.streamingBatchInterval,
        });
        await observeStream(observer, stream, (event) => events.push(event));
      }
      return events;
    }
    const stream = WorkflowStreamClient.fromWithinActivity({
      batchInterval: input.streamingBatchInterval,
    });
    const topic = stream.topic(input.streamingTopic);
    const events: ModelStreamEvent[] = [];
    try {
      for await (const event of streamWithActivityCancellation(model, messages, input.options)) {
        events.push(event);
        topic.publish(event);
      }
    } finally {
      await stream[Symbol.asyncDispose]();
    }
    return events;
  }

  // Stateful calls use their own activity types. A worker without stateful support does not
  // register them, so it cannot take such a call, drop the model state and return no updated state
  // while the agent clears its messages: the call fails there and is retried until a worker with
  // stateful support takes it.

  /** Run a stateful model with the agent's model state; return its events and the updated state. */
  async invokeStatefulModel(input: InvokeStatefulModelInput): Promise<InvokeStatefulModelResult> {
    const modelState = new StateStore(input.modelState);
    const model = this.getStatefulModel(input.modelName);
    const events = await this.runModel(model, { ...input, options: { ...input.options, modelState } });
    return { events, modelState: modelState.getAll() };
  }

  /** As {@link invokeStatefulModel}, publishing each event as {@link invokeModelStreaming} does. */
  async invokeStatefulModelStreaming(input: InvokeStatefulModelStreamingInput): Promise<InvokeStatefulModelResult> {
    const modelState = new StateStore(input.modelState);
    const model = this.getStatefulModel(input.modelName);
    const events = await this.runModelStreaming(model, { ...input, options: { ...input.options, modelState } });
    return { events, modelState: modelState.getAll() };
  }
}
