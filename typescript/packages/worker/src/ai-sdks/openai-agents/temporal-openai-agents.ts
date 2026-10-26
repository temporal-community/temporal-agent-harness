// The OpenAI Agents plugin a harness worker registers (Python's
// openai_agents/_temporal_openai_agents.py): `@temporalio/openai-agents`' plugin, with the
// harness's streamed model activity in place of its own.

import { createRequire } from "node:module";
import type { ModelProvider, StreamEvent } from "@openai/agents-core";
import { OpenAIProvider } from "@openai/agents-openai";
import {
  OpenAIAgentsPlugin as TemporalOpenAIAgentsPlugin,
  type OpenAIAgentsPluginOptions as TemporalOpenAIAgentsPluginOptions,
} from "@temporalio/openai-agents";
import type { BundleOptions, WorkerOptions } from "@temporalio/worker";
import OpenAI from "openai";
import type { ObserverFactory } from "../integration-helpers/stream-observer.ts";
import { harnessObserverFactory, type HarnessStreamToken } from "../openai-agents-harness.ts";
import {
  INVOKE_MODEL_STREAM_ACTIVITY,
  createInvokeModelStreamActivity,
} from "./invoke-model-activity.ts";
import { statelessMcpCallToolActivities } from "./mcp.ts";

export interface OpenAIAgentsPluginOptions
  extends Omit<TemporalOpenAIAgentsPluginOptions, "modelProvider"> {
  /** Resolves model names to models. Default: OpenAI, with the client's own retries off. */
  modelProvider?: ModelProvider;
  /** Builds each streamed call's observer from its routing token (harnessObserverFactory). */
  observerFactory?: ObserverFactory<HarnessStreamToken, StreamEvent>;
}

/**
 * An `OpenAIProvider` whose client does not retry, so Temporal's retry policy alone decides. The
 * client is built on first use, so a worker starts without OPENAI_API_KEY.
 */
function defaultModelProvider(): ModelProvider {
  let provider: OpenAIProvider | undefined;
  return {
    getModel: (name) =>
      (provider ??= new OpenAIProvider({ openAIClient: new OpenAI({ maxRetries: 0 }) })).getModel(
        name,
      ),
  };
}

// Workflow code must share the plugin's own copies of these. `@temporalio/openai-agents` is
// CommonJS, so it loads agents-core's CommonJS build: workflow code importing `@openai/agents-core`
// as ESM would bundle a second copy, whose classes fail the plugin's `instanceof` checks and whose
// async chunk the workflow sandbox cannot load. And the plugin's workflow interceptor hands the
// runner its config through module state in `@temporalio/openai-agents/workflow`, which a second
// copy (say, one resolved from `@temporalio/agent-harness`'s own location) would not see.
const workerRequire = createRequire(import.meta.url);
const pluginRequire = createRequire(workerRequire.resolve("@temporalio/openai-agents"));
const WORKFLOW_ALIASES = {
  "@openai/agents-core$": pluginRequire.resolve("@openai/agents-core"),
  "@temporalio/openai-agents/workflow$": workerRequire.resolve(
    "@temporalio/openai-agents/workflow",
  ),
};

type Activity = (...args: never[]) => Promise<unknown>;

/**
 * `@temporalio/openai-agents`' `OpenAIAgentsPlugin` for a harness worker: everything the official
 * plugin registers, except that `invokeModelStreamActivity` and each stateless MCP server's call
 * activity are the harness's: the first's per-call observer publishes harness turn events (see
 * `runStreamed`), the second keeps a failed call's isError flag. Also resolves workflow code's
 * `@openai/agents-core` and `@temporalio/openai-agents/workflow` to the copies the plugin uses.
 *
 * `modelParams` mean what they do on the official plugin: a client carrying this plugin sends
 * them in the `__openai_agents_config` header of the workflows it starts, where they win over the
 * workflow's own `runStreamed(..., { defaultModelParams })`.
 *
 * Not supported yet: `hostedToolCredentials` (the harness's activity does not inject them) and a
 * plugin-level `modelParams.streamingTopic` (the harness routes each run to its turn); both throw.
 */
export class OpenAIAgentsPlugin extends TemporalOpenAIAgentsPlugin {
  readonly #activities: Record<string, Activity>;

  constructor({
    modelProvider = defaultModelProvider(),
    observerFactory = harnessObserverFactory,
    ...options
  }: OpenAIAgentsPluginOptions = {}) {
    if (options.hostedToolCredentials !== undefined)
      throw new TypeError(
        "hostedToolCredentials is not supported by the harness's OpenAIAgentsPlugin yet: its " +
          "streamed model activity does not inject them. Put the credentials on the hosted tools.",
      );
    if (options.modelParams?.streamingTopic !== undefined)
      throw new TypeError(
        "modelParams.streamingTopic must be unset: runStreamed() routes each run's model events " +
          "to its own turn, and a plugin-level topic would override that route.",
      );
    super({ ...options, modelProvider });
    this.#activities = {
      ...statelessMcpCallToolActivities(options.mcpServerProviders ?? []),
      [INVOKE_MODEL_STREAM_ACTIVITY]: createInvokeModelStreamActivity(
        modelProvider,
        observerFactory,
      ),
    };
  }

  /** Registers the official activities, then the harness's over their namesakes. */
  override configureWorker(options: WorkerOptions): WorkerOptions {
    const configured = super.configureWorker(options);
    return { ...configured, activities: { ...configured.activities, ...this.#activities } };
  }

  override configureBundler(options: BundleOptions): BundleOptions {
    const configured = super.configureBundler(options);
    const hook = configured.webpackConfigHook;
    return {
      ...configured,
      webpackConfigHook: (config) => {
        const cfg = hook ? hook(config) : config;
        return {
          ...cfg,
          resolve: {
            ...cfg.resolve,
            alias: { ...cfg.resolve?.alias, ...WORKFLOW_ALIASES },
          },
        };
      },
    };
  }
}
