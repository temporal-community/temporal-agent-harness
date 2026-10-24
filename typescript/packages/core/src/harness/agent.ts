// The authoring API. `defineAgent` turns a declaration into a Temporal workflow function (export
// it under the agent's workflow type name); `handler` declares an accepted message; `tool` (inline,
// or callback when it has no `execute`) and `activityTool` declare the three tool kinds. Every tool
// publishes its own tool_start/tool_end lifecycle and passes the approval gate before it runs.
//
// Declarations are static (plain data plus zod schemas), so the web server and a parent agent can
// read an agent's handlers and init data without running it, exactly as Python reads its class.

import {
  ApplicationFailure,
  CancellationScope,
  isCancellation,
  proxyActivities,
  workflowInfo,
  type ActivityOptions,
} from "@temporalio/workflow";
import { z } from "zod";
import {
  AgentConfigSchema,
  MidTurn,
  ToolApprovalPolicy,
  toJsonSchema,
  validate,
  type AgentConfig,
  type InferInput,
  type InferOutput,
  type Schema,
  type AutoApprovalCriteria,
  type AutoModeEvaluator,
  type TurnStreamContext,
} from "./agent-protocol/index.ts";
import {
  AgentWorkflowRunner,
  currentToolCall,
  type HandlerDecl,
  type HandlerImpl,
} from "./agent-workflow.ts";
import { errorMessage, isOwnCancellation } from "./agent-workflow/errors.ts";
import { pyRepr } from "./py-repr.ts";
import type { StateDecl, StateRef } from "./state/index.ts";
import type { AnyHarnessTool, HarnessTool, ObjectSchema } from "./agent-workflow/types.ts";

export type { AnyHarnessTool, HarnessTool };

// ---------------------------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------------------------

export function handler<I extends z.ZodType, O extends z.ZodType>(decl: {
  description: string;
  input: I;
  output: O;
  midTurn?: MidTurn;
  modelCallable?: boolean;
  /**
   * The input's parameter name (a Python handler's own parameter name): a parent's subagent send
   * tool takes the input under it. Defaults to "message".
   */
  inputName?: string;
}): HandlerDecl<I, O> {
  if (!decl.description.trim())
    throw new TypeError("handler(): a description is required (it is the tool description)");
  return { midTurn: MidTurn.REJECT, modelCallable: true, inputName: "message", ...decl };
}

// ---------------------------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------------------------

/** Phantom marker for a workflow-supplied tool parameter, hidden from the model. */
export interface Injected<T> {
  readonly __injected?: T;
}
export const injected = <T>(): Injected<T> => ({});
type InjectedValues<J> = { [K in keyof J]: J[K] extends Injected<infer T> ? T : never };

interface ToolOptions<I extends ObjectSchema, O extends Schema | undefined, J> {
  name: string;
  description: string;
  inputSchema: I;
  outputSchema?: O;
  inherentlySafe?: boolean;
  autoApprovalCriteria?: string;
  injected?: J;
}

type Output<O> = O extends Schema ? InferOutput<O> : unknown;

/** Which call an inline tool's `execute` is running. */
export interface InlineToolCall {
  tool_id: string;
}

/** The context an activity tool's body runs with: which call it is, and where to publish. */
export interface AgentToolContext {
  stream_context: TurnStreamContext;
  tool_id: string;
}

/**
 * `tool_end`'s `tool_output`: the result as Python's `str()` renders it, as the Python harness
 * publishes it (a string as it is, `True`, `None`, `{'a': 1}`), until the protocol carries
 * structured results.
 */
export function toolOutputText(result: unknown): string {
  return typeof result === "string" ? result : pyRepr(result);
}

function injectedValues(
  name: string,
  names: readonly string[],
  available: Readonly<Record<string, unknown>>,
) {
  const values: Record<string, unknown> = {};
  for (const n of names) {
    if (!(n in available)) {
      throw ApplicationFailure.create({
        type: "MissingInjection",
        nonRetryable: true,
        message: `tool '${name}' requires injected argument '${n}', but runTool was called without it`,
      });
    }
    values[n] = available[n];
  }
  return values;
}

function baseTool<I extends ObjectSchema, O extends Schema | undefined, J>(
  kind: HarnessTool["kind"],
  options: ToolOptions<I, O, J>,
  body: (
    input: InferOutput<I>,
    injected: Record<string, unknown>,
    toolId: string,
    runner: AgentWorkflowRunner,
  ) => Promise<unknown>,
  publishLifecycle: boolean,
): HarnessTool<InferInput<I>, Output<O>> {
  const injectedNames = Object.keys(options.injected ?? {});
  const description = options.description;
  return {
    kind,
    name: options.name,
    description,
    inputSchema: options.inputSchema,
    outputSchema: options.outputSchema,
    inherentlySafe: options.inherentlySafe ?? false,
    autoApprovalCriteria: options.autoApprovalCriteria ?? null,
    injected: injectedNames,
    parameters: toJsonSchema(options.inputSchema),
    async invoke(raw) {
      const { runner, toolId, injections } = currentToolCall(`tool '${options.name}'`);
      const ctx = runner.currentStreamContext;
      if (ctx === null)
        throw new Error(`tool '${options.name}' must be invoked within an active turn`);
      const values = injectedValues(options.name, injectedNames, injections);
      const parsed = validate(options.inputSchema, raw);
      if (!parsed.ok) {
        throw ApplicationFailure.create({
          type: "InvalidToolInput",
          nonRetryable: true,
          message: `Payload for tool '${options.name}' does not match its schema: ${parsed.message}`,
        });
      }
      const input = parsed.value as InferOutput<I>;
      const toolInput = input as Record<string, unknown>;
      await runner.applyApprovalPolicy(toolId, options.name, toolInput, {
        inherentlySafe: options.inherentlySafe ?? false,
        description,
        autoApprovalCriteria: options.autoApprovalCriteria ?? null,
      });
      if (!publishLifecycle) return (await body(input, values, toolId, runner)) as Output<O>;
      runner.pub(ctx.turn_id, ctx.turn_number, {
        type: "tool_start",
        tool_id: toolId,
        tool_name: options.name,
        tool_input: toolInput,
      });
      let result: unknown;
      try {
        result = await body(input, values, toolId, runner);
      } catch (e) {
        if (!isOwnCancellation(e))
          runner.pub(ctx.turn_id, ctx.turn_number, {
            type: "tool_error",
            tool_id: toolId,
            tool_name: options.name,
            message: errorMessage(e),
          });
        throw e;
      }
      runner.pub(ctx.turn_id, ctx.turn_number, {
        type: "tool_end",
        tool_id: toolId,
        tool_name: options.name,
        tool_output: toolOutputText(result),
      });
      return result as Output<O>;
    },
  };
}

/**
 * An inline tool when given `execute` (it runs in the workflow: deterministic code only), or a
 * callback tool when not: the call parks in the workflow until an attached client fulfills it
 * via the `provide_callback_result` update, validated against the required `outputSchema`.
 * `execute` gets the call's `tool_id` third, to key the side effects of activities it starts on.
 */
export function tool<I extends ObjectSchema, O extends Schema | undefined = undefined, J = {}>(
  options: ToolOptions<I, O, J> & {
    execute: (
      input: InferOutput<I>,
      injected: InjectedValues<J>,
      call: InlineToolCall,
    ) => Promise<Output<O>>;
  },
): HarnessTool<InferInput<I>, Output<O>>;
export function tool<I extends ObjectSchema, O extends Schema, J = {}>(
  options: ToolOptions<I, O, J> & {
    outputSchema: O;
    execute?: undefined;
    timeout?: string | number;
  },
): HarnessTool<InferInput<I>, InferOutput<O>>;
export function tool(
  options: ToolOptions<ObjectSchema, Schema | undefined, unknown> & {
    execute?: (
      input: Record<string, unknown>,
      injected: never,
      call: InlineToolCall,
    ) => Promise<unknown>;
    timeout?: string | number;
  },
): HarnessTool<Record<string, unknown>, unknown> {
  const { execute } = options;
  if (execute !== undefined)
    return baseTool(
      "inline",
      options,
      (input, values, toolId) => execute(input, values as never, { tool_id: toolId }),
      true,
    );
  const output = options.outputSchema;
  if (output === undefined)
    throw new TypeError(
      `tool '${options.name}' has no execute, so it is a callback tool and needs an outputSchema`,
    );
  const outputSchema = toJsonSchema(output);
  return baseTool(
    "callback",
    options,
    (input, _values, toolId, runner) =>
      runner.awaitCallbackResult({
        toolId,
        toolName: options.name,
        toolInput: input,
        output,
        outputSchema,
        timeout: options.timeout,
      }),
    true,
  );
}

/**
 * A durable, activity-backed tool. This is the workflow-side declaration; register its body on a
 * worker with `toolActivity(tool, impl)` from `@temporalio/agent-harness-worker`, which also
 * publishes the call's tool_start and its tool_end or tool_error from the activity, once per
 * attempt, and heartbeats while it runs.
 *
 * Runs with a 30 s start-to-close timeout and the default retry policy unless `activityOptions`
 * replaces them (its `summary` defaults to "tool_call"). The body runs at least once and may run
 * again after a crash or timeout, so make side effects idempotent (key them on `ctx.tool_id`).
 */
export function activityTool<
  I extends ObjectSchema,
  O extends Schema | undefined = undefined,
  J = {},
>(
  options: ToolOptions<I, O, J> & { activityOptions?: ActivityOptions },
): HarnessTool<InferInput<I>, Output<O>> & {
  readonly __activity?: { input: InferOutput<I>; output: Output<O>; injected: InjectedValues<J> };
} {
  const activityOptions: ActivityOptions = {
    summary: "tool_call",
    ...(options.activityOptions ?? { startToCloseTimeout: "30 seconds" }),
  };
  return baseTool(
    "activity",
    options,
    async (input, values, toolId, runner) => {
      const ctx: AgentToolContext = {
        stream_context: runner.currentStreamContext!,
        tool_id: toolId,
      };
      const activities =
        proxyActivities<Record<string, (...args: unknown[]) => Promise<unknown>>>(activityOptions);
      return activities[options.name]!(input, values, ctx);
    },
    false,
  );
}

// ---------------------------------------------------------------------------------------------
// Agents
// ---------------------------------------------------------------------------------------------

type States = Record<string, StateDecl<z.ZodType<Record<string, unknown>>>>;
type StateRefs<S extends States> = { [K in keyof S]: StateRef<z.output<S[K]["schema"]>> };
type InitData<D> = D extends { schema: infer Z extends z.ZodType; required: infer R }
  ? R extends true
    ? z.output<Z>
    : z.output<Z> | null
  : undefined;

/**
 * An agent's init data. `serverOwned` names fields only a trusted starter may set (e.g. the task
 * queue its tools run on): the web API refuses them from clients, hides them from the published
 * schema, and takes them from `HarnessAppOptions.serverStartData` instead.
 */
export type InitDataDecl = {
  schema: z.ZodType;
  required: boolean;
  serverOwned?: readonly string[];
};

export interface AgentContext<S extends States, D> {
  runner: AgentWorkflowRunner;
  config: AgentConfig;
  data: InitData<D>;
  state: StateRefs<S>;
}

export type AgentImpl<H extends Record<string, HandlerDecl>> = {
  [K in keyof H]: HandlerImpl<H[K]>;
} & {
  /** Awaited before the first message, and again at the start of each run after a continue-as-new. */
  setup?: () => Promise<void>;
  /** Awaited once, after close drains (or on cancellation). */
  teardown?: () => Promise<void>;
};

export interface AgentDefinition<
  H extends Record<string, HandlerDecl> = Record<string, HandlerDecl>,
  S extends States = States,
  D extends InitDataDecl | undefined = InitDataDecl | undefined,
> {
  /** The workflow type. Export the result of `defineAgent` under exactly this name. */
  name: string;
  handlers: H;
  states?: S;
  initData?: D;
  approvalPolicyDefault: ToolApprovalPolicy;
  autoApprovalCriteriaDefault?: AutoApprovalCriteria;
  autoModeEvaluator?: AutoModeEvaluator;
  create(ctx: AgentContext<S, D>): AgentImpl<H>;
}

export type AgentWorkflow<Def extends AgentDefinition = AgentDefinition> = ((
  config?: unknown,
  data?: unknown,
) => Promise<void>) & {
  readonly definition: Def;
};

function invalidInput(message: string, type = "InvalidAgentInput"): ApplicationFailure {
  return ApplicationFailure.create({ type, nonRetryable: true, message });
}

export function defineAgent<
  H extends Record<string, HandlerDecl>,
  S extends States = {},
  D extends InitDataDecl | undefined = undefined,
>(definition: AgentDefinition<H, S, D>): AgentWorkflow<AgentDefinition<H, S, D>> {
  const workflow = async (
    rawConfig?: unknown,
    rawData?: unknown,
  ): Promise<void> => {
    const workflowType = workflowInfo().workflowType;
    if (workflowType !== definition.name) {
      throw invalidInput(
        `agent '${definition.name}' is registered as workflow type '${workflowType}'; export it under its own name`,
      );
    }
    const config = AgentConfigSchema.safeParse(rawConfig ?? {});
    if (!config.success)
      throw invalidInput(
        `${definition.name} was started with an invalid AgentConfig: ${config.error.message}`,
      );
    let data: unknown;
    if (definition.initData === undefined) {
      data = undefined;
    } else if (rawData === undefined || rawData === null) {
      if (definition.initData.required) {
        throw invalidInput(
          `${definition.name} requires init data, and none was given.`,
          "InitDataRequired",
        );
      }
      data = null;
    } else {
      const parsed = definition.initData.schema.safeParse(rawData);
      if (!parsed.success)
        throw invalidInput(
          `${definition.name} was started with invalid init data: ${parsed.error.message}`,
        );
      data = parsed.data;
    }
    const runner = new AgentWorkflowRunner(config.data, {
      handlers: definition.handlers,
      approvalPolicyDefault: definition.approvalPolicyDefault,
      autoApprovalCriteriaDefault: definition.autoApprovalCriteriaDefault,
      autoModeEvaluator: definition.autoModeEvaluator,
      states: definition.states,
      data,
    });
    const state = runner.state as StateRefs<S>;
    const impl = definition.create({
      runner,
      config: config.data,
      data: data as InitData<D>,
      state,
    });
    for (const name of Object.keys(definition.handlers)) {
      if (typeof impl[name] !== "function")
        throw new TypeError(
          `${definition.name}.create() returned no implementation for handler '${name}'`,
        );
    }
    await impl.setup?.();
    try {
      await runner.run(impl as Record<string, HandlerImpl<HandlerDecl>>);
    } catch (e) {
      if (isCancellation(e) && impl.teardown) await CancellationScope.nonCancellable(impl.teardown);
      throw e;
    }
    await impl.teardown?.();
  };
  Object.defineProperty(workflow, "name", { value: definition.name });
  return Object.assign(workflow, { definition });
}

/** `{ required, schema }` for an agent's init data, as `GET /api/agents` reports it; null if none. */
export function agentInitDataSchema(
  agent: AgentWorkflow,
): { required: boolean; schema: Record<string, unknown> } | null {
  const init = agent.definition.initData;
  if (init === undefined) return null;
  const schema = toJsonSchema(init.schema);
  const owned = new Set(init.serverOwned ?? []);
  if (owned.size && schema.properties && typeof schema.properties === "object") {
    schema.properties = Object.fromEntries(
      Object.entries(schema.properties).filter(([k]) => !owned.has(k)),
    );
    if (Array.isArray(schema.required))
      schema.required = schema.required.filter((k) => !owned.has(String(k)));
  }
  return { required: init.required, schema };
}
