import type { McpClient } from '@strands-agents/sdk';
import { BedrockModel, type Model } from '@strands-agents/sdk';
import type { Duration } from '@temporalio/common';
import { msOptionalToNumber } from '@temporalio/common/lib/time.js';
import type { Context as ActivityContext } from '@temporalio/activity';
import type { ActivityInboundCallsInterceptor, BundleOptions, Worker, WorkerOptions } from '@temporalio/worker';
import { SimplePlugin } from '@temporalio/plugin';
import { existsSync, readFileSync } from 'node:fs';
import { isBuiltin } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { ModelStreamEvent } from '@strands-agents/sdk';
import {
  GET_MODEL_CONFIG_ACTIVITY,
  INVOKE_STATEFUL_MODEL_ACTIVITY,
  INVOKE_STATEFUL_MODEL_STREAMING_ACTIVITY,
  callToolActivityName,
  listToolsActivityName,
  rewriteError,
  type GetModelConfigInput,
  type InvokeModelInput,
  type InvokeModelStreamingInput,
  type InvokeStatefulModelInput,
  type InvokeStatefulModelStreamingInput,
} from '@temporalio/agent-harness/ai-sdks/strands-agents';
import { ModelActivity } from './model-activity.ts';
import type { ObserverFactory } from '../integration-helpers/stream-observer.ts';
import {
  buildCallToolActivity,
  buildListToolsActivity,
  MCP_CONNECTION_IDLE_MS,
  McpConnections,
} from './temporal-mcp-client.ts';

// What each Activity a StrandsPlugin builds was built from: its name, then what it depends on.
// configureWorker lets one of these replace another with the same entries: that is the same
// instance, or one built from the same options, registered twice (e.g. on the NativeConnection
// and the Worker).
const activitySources = new WeakMap<object, readonly unknown[]>();

function sameSources(a: object, b: object): boolean {
  const x = activitySources.get(a);
  const y = activitySources.get(b);
  return x !== undefined && y !== undefined && x.length === y.length && x.every((v, i) => v === y[i]);
}

const REMEDY =
  'Rename or remove the other Activity, run the other integration on its own Worker and task queue, ' +
  'or use one StrandsPlugin instance on this Worker.';

/**
 * Options for {@link StrandsPlugin}.
 *
 * - `models` — name → factory map. Each factory is called lazily on first
 *   use and the constructed model is cached for the worker's lifetime.
 *   `TemporalAgent({ model: 'name', ... })` selects which factory to invoke.
 *   If omitted, the plugin registers a single `BedrockModel` factory under
 *   the name `'bedrock'` to match Strands' own implicit default.
 *
 * - `mcpClients` — name → MCP client factory map. Workflow-side
 *   `TemporalMCPClient({ server: 'name' })` selects a factory by name; the
 *   per-server `{name}-listTools` and `{name}-callTool` activities reuse one
 *   lazily-opened connection, which belongs to this plugin instance and closes
 *   when the last worker using the plugin stops. Tools are enumerated live on each
 *   `listTools`. By default (`TemporalMCPClient`'s `cacheTools: false`) the
 *   agent re-lists on each turn, so it picks up MCP-server changes (including
 *   redeploys) mid-workflow without restarting the worker; set `cacheTools:
 *   true` to list once at the start of the workflow instead.
 *
 * - `mcpConnectionIdleTimeout` — how long an idle MCP connection is
 *   kept open between `callTool` activities before it's disconnected. The window
 *   only starts once no activity is using the connection, so it never cuts off
 *   an in-flight call, however long it runs. Accepts a millisecond number or a
 *   duration string (e.g. `'5 minutes'`), like `startToCloseTimeout`. Defaults
 *   to {@link MCP_CONNECTION_IDLE_MS} (5 minutes).
 */
export interface StrandsPluginOptions {
  models?: Record<string, () => Model>;
  mcpClients?: Record<string, () => McpClient>;
  mcpConnectionIdleTimeout?: Duration;
  /** Harness: builds the live observer for model calls that carry a routing token. */
  observerFactory?: ObserverFactory<unknown, ModelStreamEvent>;
}

/** The plugin's model activities, whose context overflows the agent recovers from. */
const MODEL_ACTIVITIES = new Set(['invokeModel', 'invokeModelStreaming']);

/**
 * Rethrows Strands' interrupt and terminal errors from activities as typed, non-retryable
 * failures. The worker turns any other error an activity throws into a retryable
 * `ApplicationFailure` before a failure converter sees it, so this cannot be left to one.
 */
function strandsActivityInbound(ctx: ActivityContext): ActivityInboundCallsInterceptor {
  return {
    async execute(input, next) {
      try {
        return await next(input);
      } catch (err) {
        throw rewriteError(err, { benignOverflow: MODEL_ACTIVITIES.has(ctx.info.activityType) });
      }
    },
  };
}

/** Reads this package's version, which worker heartbeats report. Empty if it can't be read. */
function packageVersion(): string {
  // Harness: this copy ships inside the harness worker package, found by walking up from this file.
  try {
    let dir = dirname(fileURLToPath(import.meta.url));
    for (;;) {
      const path = join(dir, 'package.json');
      if (existsSync(path)) {
        const pkg = JSON.parse(readFileSync(path, 'utf8')) as { name?: string; version?: string };
        if (pkg.name === '@temporalio/agent-harness-worker') return pkg.version ?? '';
      }
      const parent = dirname(dir);
      if (parent === dir) return '';
      dir = parent;
    }
  } catch {
    return '';
  }
}

/**
 * Temporal plugin that runs Strands Agents inside Temporal workflows. Model,
 * MCP tool, and (via {@link workflow.activityAsTool}) custom-tool invocations
 * are routed through Temporal Activities for durable execution, retries, and
 * timeouts.
 *
 * Register on the worker:
 *
 * ```ts
 * const worker = new Worker({ ..., plugins: [new StrandsPlugin({...})] });
 * ```
 */
export class StrandsPlugin extends SimplePlugin {
  /** The package version, reported in worker heartbeats. */
  readonly version = packageVersion();

  constructor(options: StrandsPluginOptions = {}) {
    let modelFactories = options.models;
    let defaultName: string | undefined;
    if (modelFactories === undefined) {
      modelFactories = { bedrock: () => new BedrockModel({}) };
      defaultName = 'bedrock';
    } else {
      const names = Object.keys(modelFactories);
      if (names.length === 1) {
        defaultName = names[0];
      }
    }

    const modelActivity = new ModelActivity(modelFactories, defaultName, options.observerFactory);
    const activities: Record<string, (...args: never[]) => unknown> = {
      invokeModel: (input: InvokeModelInput) => modelActivity.invokeModel(input),
      invokeModelStreaming: (input: InvokeModelStreamingInput) => modelActivity.invokeModelStreaming(input),
      [INVOKE_STATEFUL_MODEL_ACTIVITY]: (input: InvokeStatefulModelInput) => modelActivity.invokeStatefulModel(input),
      [INVOKE_STATEFUL_MODEL_STREAMING_ACTIVITY]: (input: InvokeStatefulModelStreamingInput) =>
        modelActivity.invokeStatefulModelStreaming(input),
      [GET_MODEL_CONFIG_ACTIVITY]: (input: GetModelConfigInput) => modelActivity.getModelConfig(input),
    };
    // Without `models`, every plugin builds the same default.
    const modelSources = options.models === undefined ? [] : [options.models, defaultName];
    for (const [name, fn] of Object.entries(activities)) {
      activitySources.set(fn, [name, ...modelSources]);
    }

    const mcpClients = options.mcpClients ?? {};
    const idleMs = msOptionalToNumber(options.mcpConnectionIdleTimeout);
    // This plugin's own MCP connections: the Workers that share this instance share
    // them, and another StrandsPlugin opens its own, even for the same server name.
    const connections = new McpConnections(idleMs);
    for (const [server, factory] of Object.entries(mcpClients)) {
      const serverActivities = {
        [listToolsActivityName(server)]: buildListToolsActivity(server, factory, connections),
        [callToolActivityName(server)]: buildCallToolActivity(server, factory, connections),
      };
      for (const [name, fn] of Object.entries(serverActivities)) {
        activities[name] = fn;
        activitySources.set(fn, [name, factory, idleMs ?? MCP_CONNECTION_IDLE_MS]);
      }
    }

    // Closes the MCP connections once the last Worker using this plugin stops.
    const runContext = (next: () => Promise<void>): Promise<void> => connections.use(next);

    super({
      name: 'StrandsPlugin',
      activities,
      runContext,
      // A function, because the object form rebuilds the worker's interceptors without the
      // deprecated `activityInbound` key.
      workerInterceptors: (existing) => ({
        ...existing,
        activity: [...(existing?.activity ?? []), (ctx) => ({ inbound: strandsActivityInbound(ctx) })],
      }),
    });
  }

  /**
   * Adds the model and MCP Activities to the Worker.
   *
   * @throws TypeError if the Worker already has a different Activity with one of these names: a
   * caller Activity, another integration's Activity, or one from a `StrandsPlugin` that doesn't share
   * this one's `models` object, MCP client factories and idle timeout.
   */
  override configureWorker(options: WorkerOptions): WorkerOptions {
    const existing = options.activities as Record<string, unknown> | undefined;
    for (const [name, fn] of Object.entries(this.options.activities as object)) {
      const other = existing && Object.prototype.propertyIsEnumerable.call(existing, name) ? existing[name] : undefined;
      if (typeof other === 'function' && other !== fn && !sameSources(other, fn)) {
        throw new TypeError(
          `Duplicate registration of Activity '${name}': StrandsPlugin would replace a different Activity ` +
            `with this name. ${REMEDY}`
        );
      }
    }
    return super.configureWorker(options);
  }

  /**
   * Runs the Worker.
   *
   * @throws TypeError, before the Worker polls, if a plugin applied after this one replaced one of
   * its Activities with a different one. `Worker.create` applies the Worker's plugins in order, then
   * the `NativeConnection`'s.
   */
  override async runWorker(worker: Worker, next: (worker: Worker) => Promise<void>): Promise<void> {
    for (const [name, fn] of Object.entries(this.options.activities as object)) {
      const kept = worker.options.activities.get(name);
      if (kept === undefined || kept === fn || sameSources(kept, fn)) continue;
      // There is no public way to stop a Worker that has not run yet. Stopping it first means it
      // never polls; `next` then releases it as `run()` does, so `connection.close()` still works.
      (worker as unknown as { nativeWorker: { initiateShutdown(): void } }).nativeWorker.initiateShutdown();
      await next(worker).catch(() => undefined);
      throw new TypeError(
        `Duplicate registration of Activity '${name}': a plugin applied after StrandsPlugin replaced its ` +
          `Activity with this name. ${REMEDY}`
      );
    }
    return super.runWorker(worker, next);
  }

  /**
   * Extend the bundler config so workflow code can bundle `@strands-agents/sdk`:
   *
   * - Ignore worker-only / non-workflow-safe modules that the SDK index
   *   transitively pulls in but that are unreachable from workflow code.
   *   This covers:
   *   - `fs` (statically imported by `vended-plugins` and `vended-tools`).
   *   - Every `@strands-agents/sdk` model provider's HTTP SDK
   *     (`@aws-sdk/client-bedrock-runtime`, `@anthropic-ai/sdk`, `openai`,
   *     `@google/genai`, `ai`). The workers constructs models worker-side;
   *     workflow code only goes through {@link TemporalModel}.
   *   - `@aws-sdk/client-s3`, dynamically imported by the SDK's S3-backed
   *     `context-offloader` vended plugin — worker-only, never reached from
   *     workflow code.
   *   - `@temporalio/activity` and `@temporalio/client`, which the
   *     worker-only halves of {@link TemporalMCPClient}, {@link autoHeartbeat},
   *     and `@temporalio/workflow-streams/client` import.
   *   - `path` / `crypto`, pulled in transitively by the same worker-only code.
   *
   * - Replace the dynamic-imported MCP transport helpers
   *   (`@modelcontextprotocol/sdk/client/{stdio,sse,streamableHttp}.js` and
   *   their `node:*` dependencies, plus `eventsource` and
   *   `eventsource-parser/stream`) with a stub module. MCP v2 bundles its
   *   transports into the client entrypoint, so their static EventSource
   *   imports must also be stubbed. Transport I/O only runs worker-side.
   *
   * - Inline async chunks so the bundle stays a single file — webpack's
   *   default JSONP chunk loader references `self`/`document`, neither of
   *   which exists in the workflow VM.
   *
   * Harness: these modules are not added to `ignoreModules`, which would replace them for every
   * workflow in the bundle, including the user's own imports of the same libraries, and silence
   * Temporal's check on Node built-ins in the user's workflow code. Each is replaced only when
   * imported from inside `@strands-agents/sdk` or an `@modelcontextprotocol/*` package, by the
   * empty module `ignoreModules` uses. `@temporalio/activity` and `@temporalio/client` need no entry:
   * the worker-only halves that import them live in the harness worker package.
   */
  override configureBundler(options: BundleOptions): BundleOptions {
    const base = super.configureBundler(options);
    const prevHook = base.webpackConfigHook;
    return {
      ...base,
      webpackConfigHook: (config) => {
        (config.output ??= {}).asyncChunks = false;
        (config.optimization ??= {}).splitChunks = false;

        // Harness: the empty module each worker-only import from Strands is sent to.
        const resolve = (config.resolve ??= {});
        resolve.alias = { ...(resolve.alias as Record<string, unknown> | undefined), [IGNORED_PREFIX]: false };
        const plugins = [...(config.plugins ?? [])];
        plugins.push(emptyModuleRewritesPlugin() as (typeof plugins)[number]);
        // Harness: Temporal's check on Node built-ins skips those the Strands SDK imports.
        const merged = { ...config, plugins, externals: strandsExemptExternals(config.externals) };

        return prevHook ? prevHook(merged) : merged;
      },
    };
  }
}

// Harness: the request prefix the bundle aliases to `false`, Temporal's own empty module for an
// ignored import.
const IGNORED_PREFIX = '__temporal_strands_ignored__';

// Harness: the modules that importing `@strands-agents/sdk` or its MCP client pulls in and workflow
// code never uses (upstream's `ignoreModules` list and its MCP transport stubs), and where those
// imports are made from: the Strands SDK and the MCP packages it loads.
const WORKER_ONLY_MODULES =
  /^(?:node:.*|fs|path|crypto|@aws-sdk\/(?:client-bedrock-runtime|client-s3|middleware-websocket)|@anthropic-ai\/sdk|openai|@google\/genai|ai|@modelcontextprotocol\/sdk\/client\/(?:stdio|sse|streamableHttp)\.js|eventsource|eventsource-parser\/stream)$/;
const STRANDS_ISSUER = /[\\/]node_modules[\\/](?:@strands-agents[\\/]sdk|@modelcontextprotocol[\\/][^\\/]+)[\\/]/;

type Externals = Parameters<NonNullable<BundleOptions['webpackConfigHook']>>[0]['externals'];
type ExternalsFunction = (
  data: { request?: string; contextInfo?: { issuer?: string } },
  callback: unknown
) => unknown;

/**
 * Harness: Temporal's `externals` function, minus its report of a Node built-in that the Strands SDK
 * or an MCP package imports. Temporal still replaces those built-ins with an empty module; a
 * built-in imported from anywhere else is still reported.
 */
function strandsExemptExternals(externals: Externals): Externals {
  if (typeof externals !== 'function') return externals;
  const report = externals as unknown as ExternalsFunction;
  const exempt: ExternalsFunction = (data, callback) =>
    isBuiltin(data.request ?? '') && STRANDS_ISSUER.test(data.contextInfo?.issuer ?? '')
      ? Promise.resolve(undefined)
      : report(data, callback);
  return exempt as unknown as Externals;
}

/**
 * The slice of webpack's runtime API these rewrites use, read off the running
 * compiler (see {@link emptyModuleRewritesPlugin}).
 */
interface WebpackApi {
  NormalModuleReplacementPlugin: new (
    resourceRegExp: RegExp,
    // Harness: the issuer says where an import is made from.
    newResource: (resource: { request: string; contextInfo?: { issuer?: string } }) => void
  ) => WebpackPlugin;
}

interface WebpackPlugin {
  apply(compiler: WebpackCompiler): void;
}

interface WebpackCompiler {
  /** webpack 5 exposes its own runtime API on the compiler. */
  webpack: WebpackApi;
}

/**
 * A webpack plugin that stubs out `@strands-agents/sdk`'s worker-only static and
 * dynamic imports with an empty module, so they never reach the Workflow isolate.
 * Harness: every module in {@link WORKER_ONLY_MODULES}, and only when imported from
 * the Strands SDK or an MCP package; the same import from anywhere else, such as
 * the user's own workflow code, is left alone.
 *
 * webpack is taken from `compiler.webpack` rather than imported or borrowed off
 * another plugin's constructor: `@temporalio/strands-agents` doesn't depend on
 * webpack directly, and using the compiler's own webpack guarantees the exact
 * instance running the bundle regardless of how the SDK orders its own bundler
 * plugins — never a second copy.
 */
function emptyModuleRewritesPlugin(): WebpackPlugin {
  return {
    apply(compiler) {
      const { NormalModuleReplacementPlugin } = compiler.webpack;
      new NormalModuleReplacementPlugin(WORKER_ONLY_MODULES, (resource) => {
        if (STRANDS_ISSUER.test(resource.contextInfo?.issuer ?? '')) {
          resource.request = `${IGNORED_PREFIX}/${resource.request}`;
        }
      }).apply(compiler);
    },
  };
}
