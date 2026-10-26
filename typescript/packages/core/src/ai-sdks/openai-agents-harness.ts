// Harness glue for the OpenAI Agents SDK, workflow side (Python's openai_agents_harness.py, the
// half that runs in the workflow): `asOpenAIAgentTool(s)` adapting harness tools onto the SDK,
// `runStreamed` running an Agents SDK agent on `TemporalOpenAIRunner` with its model calls routed
// to the in-flight turn, and the MCP governance wrappers putting an SDK MCP server under the
// harness approval policy and tool lifecycle. The worker half (the observer that turns each model
// call's stream into turn events, and the streamed model activity that runs it) is
// `@temporalio/agent-harness-worker/ai-sdks/openai-agents-harness`.
//
// Only `@openai/agents-core` types are imported here, except `RunContext`. Workers bundle workflow
// code with the OpenAIAgentsPlugin from `@temporalio/agent-harness-worker/ai-sdks/openai-agents`,
// which resolves `@openai/agents-core` to the copy `@temporalio/openai-agents` loads.

import {
  RunContext,
  type Agent,
  type AgentInputItem,
  type CallToolResultContent,
  type FunctionTool,
  type MCPServer,
  type RunState,
  type StreamedRunResult,
} from "@openai/agents-core";
import {
  TemporalOpenAIRunner,
  statelessMcpServer as temporalStatelessMcpServer,
  type StatelessMcpServerOptions as TemporalStatelessMcpServerOptions,
  type TemporalRunOptions,
} from "@temporalio/openai-agents/workflow";
import type { ModelActivityOptions } from "@temporalio/openai-agents";
import { ApplicationFailure, AsyncLocalStorage, isCancellation, uuid4 } from "@temporalio/workflow";
import { validate, type TurnStreamContext } from "../harness/agent-protocol/index.ts";
import { ToolApprovalDenied, errorMessage } from "../harness/agent-workflow/errors.ts";
import type { AgentWorkflowRunner } from "../harness/agent-workflow.ts";
import type { AnyHarnessTool } from "../harness/agent-workflow/types.ts";

// ---------------------------------------------------------------------------------------------
// The streaming route: which turn a model call's events go to
// ---------------------------------------------------------------------------------------------

/**
 * What `runStreamed` hands the streamed model activity, serialized into the runner's
 * `streamingTopic` (an opaque string `@temporalio/openai-agents` forwards into the activity
 * input). The activity adds the requested model, which the activity input carries, to make
 * Python's `_HarnessStreamToken`.
 */
export interface HarnessStreamRoute {
  context: TurnStreamContext;
}

// ---------------------------------------------------------------------------------------------
// Tool adapter: harness tools -> OpenAI Agents SDK FunctionTools
// ---------------------------------------------------------------------------------------------

const HARNESS_TOOL_KINDS = new Set(["inline", "activity", "callback"]);

export interface OpenAIAgentToolOptions {
  /** Workflow-supplied values for the tools' injected parameters. */
  injections?: Record<string, unknown>;
  /** Send the tool's schema in OpenAI's strict mode, as the SDK's FunctionTool does (default). */
  strictJsonSchema?: boolean;
}

/**
 * Adapt a harness tool (`tool`, `activityTool` or a `subagentToolset` entry) into an OpenAI Agents
 * SDK `FunctionTool`. Every model call of it runs through `runner.runTool(...)` under the model's
 * call id, so the harness still owns the approval policy, `tool_start` / `tool_end` /
 * `tool_error`, injected parameters and activity-backed execution. A failed call goes back to the
 * model as text; an input that is not a JSON object matching the tool's schema fails the run.
 */
export function asOpenAIAgentTool(
  runner: AgentWorkflowRunner,
  tool: AnyHarnessTool,
  { injections, strictJsonSchema = true }: OpenAIAgentToolOptions = {},
): FunctionTool<AgentWorkflowRunner> {
  requireHarnessTool(tool);
  const name = tool.name;
  return {
    type: "function",
    name,
    description: tool.description,
    parameters: (strictJsonSchema
      ? ensureStrictJsonSchema(tool.parameters)
      : tool.parameters) as FunctionTool["parameters"],
    strict: strictJsonSchema,
    // Rethrow: an invalid input fails the run, as Python's on_invoke_tool raises it.
    errorFunction: null,
    needsApproval: async () => false,
    isEnabled: async () => true,
    async invoke(_runContext, input, details) {
      let json: unknown;
      try {
        json = JSON.parse(input || "{}");
      } catch {
        throw invalidToolInput(`Invalid JSON input for tool ${name}: ${input}`);
      }
      if (json === null || typeof json !== "object" || Array.isArray(json)) {
        const got = Array.isArray(json) ? "list" : typeof json;
        throw invalidToolInput(`Tool ${name} expected a JSON object input, got ${got}.`);
      }
      const parsed = validate(tool.inputSchema, json);
      if (!parsed.ok)
        throw invalidToolInput(
          `Payload for tool '${name}' does not match its schema: ${parsed.message}`,
        );
      const callId = details?.toolCall?.callId || newToolCallId(name);
      let result: unknown;
      try {
        result = await runner.runTool(callId, tool, json, injections ? { injections } : {});
      } catch (e) {
        if (isCancellation(e)) throw e;
        return `Tool '${name}' failed: ${errorMessage(e)}`;
      }
      return typeof result === "string" ? result : JSON.stringify(result ?? null);
    },
  };
}

/** Adapt several harness tools; see `asOpenAIAgentTool`. */
export function asOpenAIAgentTools(
  runner: AgentWorkflowRunner,
  tools: readonly AnyHarnessTool[] | Readonly<Record<string, AnyHarnessTool>>,
  options?: OpenAIAgentToolOptions,
): FunctionTool<AgentWorkflowRunner>[] {
  const list: readonly AnyHarnessTool[] = Array.isArray(tools)
    ? tools
    : Object.values(tools as Readonly<Record<string, AnyHarnessTool>>);
  return list.map((t) => asOpenAIAgentTool(runner, t, options));
}

function requireHarnessTool(tool: AnyHarnessTool): void {
  const candidate = tool as Partial<AnyHarnessTool> | null;
  if (
    candidate !== null &&
    typeof candidate === "object" &&
    HARNESS_TOOL_KINDS.has(candidate.kind ?? "") &&
    typeof candidate.invoke === "function"
  )
    return;
  throw new TypeError(
    `${String(candidate?.name ?? candidate)} is not a harness tool; declare it with tool(...), ` +
      "activityTool(...), or use subagentToolset(...).",
  );
}

function invalidToolInput(message: string): ApplicationFailure {
  return ApplicationFailure.create({ type: "InvalidToolInput", nonRetryable: true, message });
}

function newToolCallId(toolName: string): string {
  return `openai_agents:${toolName}:${uuid4()}`;
}

type JsonSchema = Record<string, unknown>;

const isSchemaObject = (v: unknown): v is JsonSchema =>
  v !== null && typeof v === "object" && !Array.isArray(v);

/**
 * The Agents SDK's `ensure_strict_json_schema` (Python), which OpenAI's strict mode needs: every
 * object closed and every property required, a lone `allOf` inlined, `default: null` dropped, and
 * a `$ref` with siblings expanded. Returns a copy.
 */
function ensureStrictJsonSchema(schema: JsonSchema): JsonSchema {
  if (Object.keys(schema).length === 0)
    return { additionalProperties: false, type: "object", properties: {}, required: [] };
  const root = JSON.parse(JSON.stringify(schema)) as JsonSchema;
  return strictify(root, root);
}

function strictify(node: JsonSchema, root: JsonSchema): JsonSchema {
  for (const key of ["$defs", "definitions"]) {
    const defs = node[key];
    if (isSchemaObject(defs))
      for (const [name, def] of Object.entries(defs))
        if (isSchemaObject(def)) defs[name] = strictify(def, root);
  }
  if (node.type === "object") {
    if (!("additionalProperties" in node)) node.additionalProperties = false;
    else if (node.additionalProperties)
      throw new TypeError(
        "additionalProperties should not be set for object types. This could be because you're " +
          "using an older version of Pydantic, or because you configured additional properties " +
          "to be allowed. If you really need this, set strictJsonSchema: false.",
      );
  }
  const properties = node.properties;
  if (isSchemaObject(properties)) {
    node.required = Object.keys(properties);
    for (const [name, prop] of Object.entries(properties))
      if (isSchemaObject(prop)) properties[name] = strictify(prop, root);
  }
  if (isSchemaObject(node.items)) node.items = strictify(node.items, root);
  for (const key of ["anyOf", "oneOf"]) {
    const variants = node[key];
    if (Array.isArray(variants))
      node[key] = variants.map((v) => (isSchemaObject(v) ? strictify(v, root) : v));
  }
  const allOf = node.allOf;
  if (Array.isArray(allOf)) {
    if (allOf.length === 1 && isSchemaObject(allOf[0])) {
      Object.assign(node, strictify(allOf[0], root));
      delete node.allOf;
    } else {
      node.allOf = allOf.map((v) => (isSchemaObject(v) ? strictify(v, root) : v));
    }
  }
  if ("default" in node && node.default === null) delete node.default;
  const ref = node.$ref;
  if (typeof ref === "string" && Object.keys(node).length > 1) {
    const resolved = resolveRef(root, ref);
    const expanded: JsonSchema = { ...resolved, ...node };
    delete expanded.$ref;
    for (const key of Object.keys(node)) delete node[key];
    Object.assign(node, expanded);
    return strictify(node, root);
  }
  return node;
}

function resolveRef(root: JsonSchema, ref: string): JsonSchema {
  if (!ref.startsWith("#/"))
    throw new TypeError(`Unexpected $ref format '${ref}'; does not start with #/`);
  let node: unknown = root;
  for (const part of ref.slice(2).split("/")) {
    if (!isSchemaObject(node)) break;
    node = node[part];
  }
  if (!isSchemaObject(node)) throw new TypeError(`Encountered non-object $ref target '${ref}'`);
  return node;
}

// ---------------------------------------------------------------------------------------------
// The streamed run
// ---------------------------------------------------------------------------------------------

export type RunStreamedOptions = Omit<
  TemporalRunOptions<AgentWorkflowRunner>,
  "stream" | "context"
> & {
  /**
   * The `TemporalOpenAIRunner`'s `defaultModelParams`: model activity options written in the
   * workflow, layered over the package defaults and under the `modelParams` of a client carrying
   * the OpenAIAgentsPlugin. `streamingTopic` is the harness's turn route and cannot be set.
   */
  defaultModelParams?: Omit<ModelActivityOptions, "streamingTopic">;
};

// oxlint-disable-next-line no-explicit-any -- agents-core's own Agent bound
type AnyAgent = Agent<any, any>;

/**
 * Run `agent` with streaming on the `TemporalOpenAIRunner`, every model call's events published
 * to the current turn by the streamed model activity: `model_interaction_started`,
 * `reply_delta`, `thought_summary`, `text_annotation`, `tool_requested`, then
 * `model_interaction_ended`. The run context is `runner`, as Python authors pass
 * `Runner.run_streamed(..., context=self._runner)`. Iterate the result (or await
 * `result.completed`) to drive the run to its end.
 *
 * Refuses an agent whose tools are raw functions, or whose MCP servers were not built by a
 * harness MCP factory (`statelessMcpServer`) or wrapped with `asHarnessMcpServer` and marked
 * durable.
 */
export async function runStreamed<TAgent extends AnyAgent>(
  runner: AgentWorkflowRunner,
  agent: TAgent,
  input: string | AgentInputItem[] | RunState<AgentWorkflowRunner, TAgent>,
  { defaultModelParams, ...options }: RunStreamedOptions = {},
): Promise<StreamedRunResult<AgentWorkflowRunner, TAgent>> {
  const context = runner.currentStreamContext;
  if (context === null)
    throw new Error("runStreamed() must be called within an active turn, from a message handler");
  for (const t of agent.tools) {
    if (typeof t === "function")
      throw new TypeError(
        "Provided tool is not a tool type. If using an activity, make sure to wrap it with " +
          "activityAsTool from @temporalio/openai-agents/workflow.",
      );
  }
  const starting = await withMcpCallIds(agent, runner);
  const route: HarnessStreamRoute = { context };
  const temporalRunner = new TemporalOpenAIRunner({
    defaultModelParams: { ...defaultModelParams, streamingTopic: JSON.stringify(route) },
  });
  return temporalRunner.run(starting, input, { ...options, context: runner, stream: true });
}

// ---------------------------------------------------------------------------------------------
// MCP adapter: harness gating + tool lifecycle events for an SDK MCP server
// ---------------------------------------------------------------------------------------------
//
// `asHarnessMcpServer` wraps the server's `callTool` so the call funnels through `runTool` exactly
// like a harness tool: policy gate first, then tool_start -> tool_end / tool_error.

const MCP_GOVERNED = Symbol.for("temporal.harness/mcp-governed");
const MCP_DURABLE = Symbol.for("temporal.harness/mcp-durable");

type MarkedServer = MCPServer & { [MCP_GOVERNED]?: true; [MCP_DURABLE]?: true };

// agents-core hands `callTool` no call id (its meta resolver sees only the run context), so
// `runStreamed` wraps each MCP function tool to put the model's call id in scope here.
const mcpCallId = new AsyncLocalStorage<string>();

/**
 * Declare that `server` replays durably, and return it. `runStreamed` rejects an MCP server
 * without this mark: a plain SDK server (stdio, HTTP) re-runs its tool calls on replay. Mark only a
 * server whose calls go through an activity or a Nexus operation.
 */
export function markDurableMcpServer<S extends MCPServer>(server: S): S {
  (server as MarkedServer)[MCP_DURABLE] = true;
  return server;
}

/** Whether `server` was marked by `markDurableMcpServer`. */
export const isDurableMcpServer = (server: MCPServer): boolean =>
  (server as MarkedServer)[MCP_DURABLE] === true;

/** Whether `server` is already under harness governance. */
export const isHarnessMcpServer = (server: MCPServer): boolean =>
  (server as MarkedServer)[MCP_GOVERNED] === true;

export interface HarnessMcpOptions {
  /** Every tool on this server is safe under any input; the policy decides what that skips. */
  inherentlySafe?: boolean;
  /**
   * The criteria set AUTO MODE judges this server's tools against. `AutoApprovalCriteria.tools`
   * still assigns per tool name.
   */
  autoApprovalCriteria?: string;
}

/**
 * Put an OpenAI Agents SDK MCP server under harness tool governance. Returns the same server with
 * its `callTool` wrapped, so every MCP tool call is evaluated against the agent's
 * ToolApprovalPolicy (waiting for a human when it does not auto-approve) and publishes
 * `tool_start`, then `tool_end` or `tool_error`, under the model's call id. A denied call returns
 * an `isError` result to the model instead of throwing. Wrapping twice is a no-op.
 */
export function asHarnessMcpServer<S extends MCPServer>(
  server: S,
  runner: AgentWorkflowRunner,
  { inherentlySafe = false, autoApprovalCriteria }: HarnessMcpOptions = {},
): S {
  if (isHarnessMcpServer(server)) return server;
  const inner = server.callTool.bind(server);
  server.callTool = (toolName, args, meta, options) => {
    const toolId = mcpCallId.getStore() ?? newToolCallId(toolName);
    const toolInput = { ...args };
    const invoke = async (): Promise<CallToolResultContent> => {
      try {
        await runner.applyApprovalPolicy(toolId, toolName, toolInput, {
          inherentlySafe,
          description: null,
          autoApprovalCriteria: autoApprovalCriteria ?? null,
        });
      } catch (e) {
        // The gate already published tool_approval_resolved(approved=false); the call never
        // starts, so it gets no tool_start/tool_error bracket.
        if (!(e instanceof ToolApprovalDenied)) throw e;
        return mcpErrorResult(e.message);
      }
      runner.publish({
        type: "tool_start",
        tool_id: toolId,
        tool_name: toolName,
        tool_input: toolInput,
      });
      let result: CallToolResultContent;
      try {
        result = await inner(toolName, args, meta, options);
      } catch (e) {
        // Close the bracket, then let the error travel on, as the tool wrappers do.
        runner.publish({
          type: "tool_error",
          tool_id: toolId,
          tool_name: toolName,
          message: errorMessage(e),
        });
        throw e;
      }
      // An MCP server reports a failed call in the result, not by raising.
      const text = mcpResultText(result);
      runner.publish(
        result.isError
          ? { type: "tool_error", tool_id: toolId, tool_name: toolName, message: text }
          : { type: "tool_end", tool_id: toolId, tool_name: toolName, tool_output: text },
      );
      return result;
    };
    return runner.runTool(toolId, { name: toolName, invoke }, undefined);
  };
  (server as MarkedServer)[MCP_GOVERNED] = true;
  return server;
}

/** Put several MCP servers under harness tool governance; see `asHarnessMcpServer`. */
export const asHarnessMcpServers = (
  servers: Iterable<MCPServer>,
  runner: AgentWorkflowRunner,
  options?: HarnessMcpOptions,
): MCPServer[] => [...servers].map((s) => asHarnessMcpServer(s, runner, options));

export type StatelessMcpServerOptions = TemporalStatelessMcpServerOptions &
  Pick<HarnessMcpOptions, "inherentlySafe"> & {
    /** The agent's runner: governance needs it, and the SDK calls `callTool` with no context. */
    runner: AgentWorkflowRunner;
  };

/**
 * A governed, durable stateless MCP server: each list and call runs as an activity against the
 * `StatelessMCPServerProvider` of the same `name` registered on the worker's OpenAIAgentsPlugin,
 * under the harness approval policy and tool lifecycle. Lists its tools anew per run unless
 * `cacheToolsList` (default false).
 */
export function statelessMcpServer(
  name: string,
  { runner, inherentlySafe, cacheToolsList = false, ...options }: StatelessMcpServerOptions,
): MCPServer {
  const server = temporalStatelessMcpServer(name, { ...options, cacheToolsList });
  // The worker's call activity is the harness's, which returns `{content, isError}`: the content
  // array alone would lose the server's isError flag to serialization.
  const callActivity = server.callTool.bind(server);
  server.callTool = async (toolName, args, meta, callOptions) =>
    fromCallToolWire(await callActivity(toolName, args, meta, callOptions));
  return asHarnessMcpServer(markDurableMcpServer(server), runner, { inherentlySafe });
}

/** The harness MCP call activity's `{content, isError}` as agents-core's content-with-flag. */
function fromCallToolWire(wire: unknown): CallToolResultContent {
  if (Array.isArray(wire)) return wire as CallToolResultContent;
  const { content, isError } = wire as { content: CallToolResultContent; isError?: boolean };
  return isError ? Object.assign(content, { isError: true }) : content;
}

function mcpResultText(content: CallToolResultContent): string {
  const texts = content.flatMap((block) =>
    block.type === "text" && typeof block.text === "string" ? [block.text] : [],
  );
  return texts.length ? texts.join("\n") : JSON.stringify(content);
}

function mcpErrorResult(message: string): CallToolResultContent {
  return Object.assign([{ type: "text" as const, text: message }], { isError: true });
}

/**
 * Validate `agent`'s MCP servers and turn them into plain function tools whose calls run with the
 * model's call id in scope, so a governed server's events share the `tool_id` of the model's
 * `tool_requested`. The servers' tools are listed here, once per run.
 */
async function withMcpCallIds<TAgent extends AnyAgent>(
  agent: TAgent,
  runner: AgentWorkflowRunner,
): Promise<TAgent> {
  if (agent.mcpServers.length === 0) return agent;
  for (const s of agent.mcpServers) {
    if (!isDurableMcpServer(s))
      throw new TypeError(
        `Unknown mcp_server '${s.name}' may not work durably. Build it with a harness ` +
          "mcp_server factory.",
      );
    if (!isHarnessMcpServer(s))
      throw new TypeError(
        `MCP server '${s.name}' was not built by a harness mcp_server factory. Build it ` +
          "with one, or wrap it with asHarnessMcpServer().",
      );
  }
  // Fresh tool objects on every listing, so wrapping them in place touches no shared state.
  const tools = await agent.getMcpTools(new RunContext(runner));
  for (const t of tools) {
    if (t.type !== "function") continue;
    const invoke = t.invoke;
    t.invoke = (ctx, input, details) => {
      const callId = details?.toolCall?.callId;
      return callId
        ? mcpCallId.run(callId, () => invoke(ctx, input, details))
        : invoke(ctx, input, details);
    };
  }
  return agent.clone({ mcpServers: [], tools: [...agent.tools, ...tools] }) as TAgent;
}
