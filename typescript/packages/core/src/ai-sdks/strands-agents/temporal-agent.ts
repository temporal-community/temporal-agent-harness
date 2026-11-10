import { Agent, AfterToolsEvent } from '@strands-agents/sdk';
import type { AgentConfig, AgentResult, AgentStreamEvent, InvokeArgs, InvokeOptions } from '@strands-agents/sdk';
import type { ActivityOptions } from '@temporalio/workflow';
import type { Duration } from '@temporalio/common/lib/time.js';
import { TemporalModel, type TemporalModelOptions } from './temporal-model.ts';
import { TemporalMCPClient } from './temporal-mcp-client.ts';
import { workflowErrorFor } from './strands-errors.ts';

const SNAPSHOT_DISABLED =
  'TemporalAgent disables takeSnapshot()/loadSnapshot(). Temporal workflows ' +
  'already persist agent state durably via the event history at a finer ' +
  'granularity than Strands snapshots. Remove the snapshot call and rely on ' +
  "Temporal's durable execution instead.";

// Strands' own message, which its Agent throws at construction for a stateful model.
const STATEFUL_MANAGER_REFUSED =
  'contextManager and conversationManager cannot be used with a stateful model. ' +
  'The model manages conversation state server-side.';

const RETRY_STRATEGY_DISABLED =
  'TemporalAgent disables Strands retries; configure retries via retryPolicy ' +
  'in the model activity options on TemporalAgent, and on the activity options ' +
  'passed to workflow.activityAsTool, workflow.activityAsHook, or TemporalMCPClient. ' +
  'Remove retryStrategy from TemporalAgent(...) or pass retryStrategy: null.';

/**
 * Options for {@link TemporalAgent}.
 *
 * Accepts everything {@link AgentConfig} accepts except `model` (which is a
 * factory name selecting a worker-side {@link Model}, not an instance) and
 * `retryStrategy` (always disabled — Temporal handles retries via
 * `activityOptions.retry`).
 *
 * The {@link ActivityOptions} apply to every model invocation this agent
 * makes. `streamingTopic`/`streamingBatchInterval` route each
 * {@link ModelStreamEvent} to a {@link WorkflowStream} topic of that name.
 */
export interface TemporalAgentOptions extends Omit<AgentConfig, 'model' | 'retryStrategy'> {
  model?: string;
  activityOptions?: ActivityOptions;
  streamingTopic?: string;
  streamingBatchInterval?: Duration;
  /** Harness: see {@link TemporalModelOptions.streamRoute}. */
  streamRoute?: TemporalModelOptions['streamRoute'];
}

/**
 * A Strands {@link Agent} that routes every model call through a Temporal
 * activity.
 *
 * `model` is the name of a factory registered in
 * `StrandsPlugin({ models: {...} })`. The `activityOptions` apply to every
 * model invocation this agent makes. All other options are forwarded to
 * Strands' {@link Agent} constructor (`tools`, `plugins`, `systemPrompt`,
 * `structuredOutputSchema`, `messages`, etc.).
 *
 * Strands' `retryStrategy` is disabled; configure retries via
 * `activityOptions.retry` here and on the activity options accepted by
 * {@link activityAsTool}, {@link activityAsHook}, and {@link TemporalMCPClient}.
 *
 * Cancelling a model activity aborts the model call only when
 * `activityOptions.heartbeatTimeout` is set, since the worker learns of the
 * cancellation through the activity's heartbeats, and only when the model
 * passes `cancelSignal` to its client (Strands' `AnthropicModel` does not). A
 * model that ignores it keeps running and its activity can still complete with
 * the model's result. A workflow cancel during a model call cancels the model
 * activity. With `TRY_CANCEL` the run ends as cancelled at once. With
 * `WAIT_CANCELLATION_COMPLETED` it ends as cancelled only if the activity
 * does; if the activity completes, the turn continues with the model's result.
 */
export class TemporalAgent extends Agent {
  private readonly managerGiven: boolean;

  constructor(options: TemporalAgentOptions = {}) {
    const { model, activityOptions, streamingTopic, streamingBatchInterval, streamRoute, ...rest } = options;
    if ((rest as { retryStrategy?: unknown }).retryStrategy !== undefined) {
      throw new Error(RETRY_STRATEGY_DISABLED);
    }
    const temporalModel = new TemporalModel({
      modelName: model,
      activityOptions,
      streamingTopic,
      streamingBatchInterval,
      streamRoute,
    });
    super({
      ...rest,
      model: temporalModel,
      retryStrategy: null,
    });
    this.managerGiven = Boolean(rest.conversationManager || rest.contextManager);

    // Strands lists each MCP client's tools once at agent initialization and
    // reuses that list for every turn. For clients with `cacheTools: false`,
    // re-list on each turn so a mid-workflow MCP-server restart is picked up.
    // `AfterToolsEvent` fires after a tool round completes, before the next
    // model call reads the tool registry — so the refresh lands on that call.
    // One hook per client, as each lists its server. Strands runs them last to
    // first, so the first client's hook runs last: it checks the round's
    // listings together and applies them, so a name can move from one server
    // to another.
    const mcpClients = collectMcpClients(rest.tools);
    for (const hook of TemporalMCPClient.refreshHooks(mcpClients.filter((client) => !client.cacheTools))) {
      this.addHook(AfterToolsEvent, hook);
    }
    // A name that Strands adds a client's tool under is that client's until a
    // re-listing of that client drops it.
    TemporalMCPClient.holdAdded(mcpClients, this.toolRegistry);
  }

  /**
   * Read the worker model's id, context window and stateful flag before Strands initializes the
   * agent, so its traces, proactive compression and stateful handling use them from the first
   * model call.
   *
   * Not `async`: once the config is read, this returns Strands' own `initialize()` promise, so it
   * adds no microtask before Strands' hooks and model call.
   */
  override initialize(): Promise<void> {
    const loading = this.model instanceof TemporalModel ? this.model.loadConfig() : undefined;
    return loading ? loading.then(() => this.initializeWithConfig()) : this.initializeWithConfig();
  }

  private initializeWithConfig(): Promise<void> {
    // Strands' Agent refuses these managers for a stateful model at construction, before
    // TemporalAgent can read the worker model's stateful flag, so refuse them here.
    if (this.model.stateful && this.managerGiven) {
      return Promise.reject(new Error(STATEFUL_MANAGER_REFUSED));
    }
    return super.initialize();
  }

  /**
   * Strands' stream, ending in a Temporal failure where Strands would end in an error that only
   * fails the workflow task: a terminal model error becomes a typed, non-retryable failure, and a
   * model activity that failed or was cancelled ends the turn with that activity's failure. So the
   * run, or the update that runs the turn, fails or is cancelled. `invoke()` uses this too.
   *
   * Its awaits add microtasks to each agent event, and those are part of replay: when two turns run
   * at once in one workflow, they can change the order in which the turns schedule activities. So
   * removing this override, or wrapping the stream in another layer, can break replay of such runs.
   */
  override async *stream(
    args: InvokeArgs,
    options?: InvokeOptions
  ): AsyncGenerator<AgentStreamEvent, AgentResult, undefined> {
    try {
      return yield* super.stream(args, options);
    } catch (err) {
      throw workflowErrorFor(err);
    }
  }

  override takeSnapshot(): never {
    throw new Error(SNAPSHOT_DISABLED);
  }

  override loadSnapshot(): never {
    throw new Error(SNAPSHOT_DISABLED);
  }
}

/** Collect every {@link TemporalMCPClient} in a (possibly nested) tool list. */
function collectMcpClients(tools: AgentConfig['tools']): TemporalMCPClient[] {
  const found: TemporalMCPClient[] = [];
  const walk = (items: NonNullable<AgentConfig['tools']>): void => {
    for (const item of items) {
      if (Array.isArray(item)) walk(item);
      else if (item instanceof TemporalMCPClient) found.push(item);
    }
  };
  if (tools) walk(tools);
  return found;
}
